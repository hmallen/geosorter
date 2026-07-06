"""GeoNames bootstrap: download + load the cities/admin/country reference data.

Phase 0a loads **populated places** (``cities500``) plus the admin-code and
country lookups needed to format ``"City, Region, Country"`` place strings.
Feature classes L/T/H (parks/peaks/hydro) arrive in a later task (B5).

``load()`` is pure file-IO + SQLite and is fully unit-tested against fixtures.
``download()`` is network glue (resumable HTTP + unzip) and is exercised
manually, not in the test suite.
"""

from __future__ import annotations

import shutil
import zipfile
from collections.abc import Callable
from pathlib import Path

import httpx

from . import db

GEONAMES_BASE = "https://download.geonames.org/export/dump/"

# remote filename per logical source
_REMOTE_FILES = {
    "cities500": "cities500.zip",
    "admin1": "admin1CodesASCII.txt",
    "admin2": "admin2Codes.txt",
    "countries": "countryInfo.txt",
}

# allCountries is the only GeoNames dump carrying L/T/H features; fetched only
# when --features is requested (~400 MB zip vs cities500's ~10 MB).
_FEATURES_REMOTE = "allCountries.zip"

# Curated GeoNames feature codes kept from classes L (parks/areas), T (peaks),
# and H (hydro). The point is meaningful wilderness folder names — a named park,
# peak, or major lake — without burying captures under every creek and hillock.
# Codes per https://www.geonames.org/export/codes.html. Override via
# ``load(..., feature_codes=...)``.
DEFAULT_FEATURE_CODES: frozenset[str] = frozenset(
    {
        # L — parks & protected areas
        "PRK", "RES", "RESN", "RESW", "RESF", "RESV",
        # T — peaks & mountains
        "MT", "PK", "PKS", "MTS", "VLC",
        # H — major water bodies & falls
        "LK", "LKS", "RSV", "FLLS", "BAY",
    }
)

ProgressFn = Callable[[str, int, int], None]


# --------------------------------------------------------------------------- #
# Parsing + loading (unit-tested)
# --------------------------------------------------------------------------- #
def _rows(path: Path, *, skip_comments: bool = False):
    """Yield tab-split field lists from a GeoNames TSV file."""
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n").rstrip("\r")
            if not line:
                continue
            if skip_comments and line.startswith("#"):
                continue
            yield line.split("\t")


def _to_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_geonames_row(f: list[str]) -> tuple | None:
    """Map one GeoNames TSV field list to a ``geonames`` row tuple.

    Returns ``None`` for short/malformed lines (the standard dump has 19 columns)
    and for rows whose lat/lon fields aren't numeric — one corrupt line in a
    ~12M-row dump must skip that row, never abort the whole bootstrap. Shared by
    the cities and the feature loaders — both consume the same layout.
    """
    if len(f) < 18:
        return None
    try:
        lat = float(f[4])
        lon = float(f[5])
    except (TypeError, ValueError):
        return None
    return (
        _to_int(f[0]),  # geonameid
        f[1],  # name
        f[2],  # ascii_name
        lat,
        lon,
        f[6] or None,  # feature_class
        f[7] or None,  # feature_code
        f[8] or None,  # country_code
        f[10] or None,  # admin1_code
        f[11] or None,  # admin2_code
        _to_int(f[14]),  # population
        f[17] or None,  # timezone
    )


_INSERT_GEONAMES = (
    "INSERT OR REPLACE INTO geonames "
    "(geonameid, name, ascii_name, lat, lon, feature_class, feature_code, "
    " country_code, admin1_code, admin2_code, population, timezone) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _populate_spatial_index(conn, spatial_index: str) -> None:
    """Repopulate the rtree from every ``geonames`` row (no-op in columnar mode).

    Idempotent and run once after all data loads so the index covers the union of
    cities and features. The columnar ``(lat, lon)`` index is maintained
    automatically by SQLite, so nothing is needed there.
    """
    if spatial_index == "rtree":
        conn.execute(
            "INSERT OR REPLACE INTO geonames_rtree(id, min_lat, max_lat, min_lon, max_lon) "
            "SELECT geonameid, lat, lat, lon, lon FROM geonames"
        )


def _load_cities(conn, path: Path) -> int:
    rows = [t for f in _rows(path) if (t := _parse_geonames_row(f)) is not None]
    conn.executemany(_INSERT_GEONAMES, rows)
    return len(rows)


def _load_features(conn, path: Path, codes: frozenset[str]) -> int:
    """Load only L/T/H rows whose ``feature_code`` is in ``codes`` from a dump.

    Parses ``allCountries.txt`` (same 19-column layout as cities500) and keeps a
    row only when its feature class is L, T, or H *and* its feature code is in the
    curated allowlist — so populated places and unlisted minor features are dropped.
    """
    rows = []
    for f in _rows(path):
        t = _parse_geonames_row(f)
        if t is None:
            continue
        feature_class, feature_code = t[5], t[6]
        if feature_class in ("L", "T", "H") and feature_code in codes:
            rows.append(t)
    conn.executemany(_INSERT_GEONAMES, rows)
    return len(rows)


def _load_admin(conn, path: Path, table: str) -> int:
    rows = [
        (f[0], f[1], f[2], _to_int(f[3]))
        for f in _rows(path)
        if len(f) >= 4
    ]
    conn.executemany(
        f"INSERT OR REPLACE INTO {table}(code, name, ascii_name, geonameid) "
        "VALUES (?,?,?,?)",
        rows,
    )
    return len(rows)


def _load_countries(conn, path: Path) -> int:
    rows = []
    for f in _rows(path, skip_comments=True):
        if len(f) < 17:
            continue
        rows.append((f[0], f[4], _to_int(f[16])))  # ISO, Country, geonameid
    conn.executemany(
        "INSERT OR REPLACE INTO country_info(country_code, country_name, geonameid) "
        "VALUES (?,?,?)",
        rows,
    )
    return len(rows)


def load(
    geonames_db: str | Path,
    src_dir: str | Path,
    *,
    spatial_index: str = "rtree",
    features: bool = False,
    feature_codes: frozenset[str] | set[str] | None = None,
) -> dict[str, int]:
    """Load GeoNames reference data from ``src_dir`` into the geonames DB.

    Expects ``cities500.txt``, ``admin1CodesASCII.txt``, ``admin2Codes.txt`` and
    ``countryInfo.txt`` in ``src_dir``. Idempotent (``INSERT OR REPLACE``).
    Returns a count per source.

    When ``features`` is true, additionally parses ``allCountries.txt`` from
    ``src_dir`` and loads its L/T/H rows whose feature code is in ``feature_codes``
    (defaulting to :data:`DEFAULT_FEATURE_CODES`), adding a ``"features"`` count.
    The spatial index is populated once at the end so it covers cities + features.
    """
    src = Path(src_dir)
    codes = frozenset(feature_codes) if feature_codes is not None else DEFAULT_FEATURE_CODES
    conn = db.connect(geonames_db, integrity_check=False)
    try:
        db.init_geonames_schema(conn, spatial_index=spatial_index)
        counts = {
            "geonames": _load_cities(conn, src / "cities500.txt"),
            "admin1": _load_admin(conn, src / "admin1CodesASCII.txt", "admin1_codes"),
            "admin2": _load_admin(conn, src / "admin2Codes.txt", "admin2_codes"),
            "countries": _load_countries(conn, src / "countryInfo.txt"),
        }
        if features:
            counts["features"] = _load_features(conn, src / "allCountries.txt", codes)
        _populate_spatial_index(conn, spatial_index)
        conn.commit()
    finally:
        conn.close()
    return counts


# --------------------------------------------------------------------------- #
# Download (network glue — not unit-tested)
# --------------------------------------------------------------------------- #
def _download_file(
    url: str, target: Path, *, resume: bool = True, progress: ProgressFn | None = None
) -> Path:
    headers: dict[str, str] = {}
    existing = target.stat().st_size if target.exists() else 0
    if resume and existing:
        headers["Range"] = f"bytes={existing}-"
    with httpx.stream(
        "GET", url, headers=headers, follow_redirects=True, timeout=60.0
    ) as resp:
        if resp.status_code == 416:  # range not satisfiable -> already complete
            return target
        resp.raise_for_status()

        # Only treat the response as a resume if the server actually honoured the
        # Range request AND resumed from our exact offset. A 200 (Range ignored)
        # or a mismatched Content-Range means we must restart the file from
        # scratch — appending would corrupt it.
        partial = resp.status_code == 206 and resp.headers.get(
            "Content-Range", ""
        ).startswith(f"bytes {existing}-")

        downloaded = existing if partial else 0
        mode = "ab" if partial else "wb"
        length = resp.headers.get("Content-Length")
        # total stays 0 (unknown) when Content-Length is absent (chunked), so the
        # progress callback shows raw bytes rather than a bogus percentage.
        total = (int(length) + downloaded) if length is not None else 0
        with open(target, mode) as fh:
            for chunk in resp.iter_bytes(65536):
                fh.write(chunk)
                downloaded += len(chunk)
                if progress:
                    progress(target.name, downloaded, total)
    return target


def download(
    dest_dir: str | Path,
    *,
    base_url: str = GEONAMES_BASE,
    min_free_mb: int = 300,
    resume: bool = True,
    progress: ProgressFn | None = None,
    features: bool = False,
) -> Path:
    """Download the GeoNames source files into ``dest_dir`` and extract cities500.

    Performs a disk-space pre-flight, downloads each file (resumable), and
    unzips ``cities500.zip`` to ``cities500.txt``. Returns ``dest_dir`` (which
    can then be passed to :func:`load`).

    When ``features`` is true, additionally fetches and unzips the much larger
    ``allCountries.zip`` to ``allCountries.txt`` (the only dump carrying L/T/H
    features); the disk pre-flight margin is raised accordingly.
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    # allCountries is ~400 MB zipped and ~1.7 GB extracted; budget far more room.
    needed_mb = (min_free_mb + 2200) if features else min_free_mb
    free = shutil.disk_usage(dest).free
    if free < needed_mb * 1024 * 1024:
        raise OSError(
            f"insufficient disk space at {dest}: "
            f"{free // (1024 * 1024)} MiB free, need >= {needed_mb} MiB"
        )

    for fname in _REMOTE_FILES.values():
        _download_file(base_url + fname, dest / fname, resume=resume, progress=progress)

    zip_path = dest / "cities500.zip"
    with zipfile.ZipFile(zip_path) as zf:
        zf.extract("cities500.txt", dest)
    # Drop the archive so it does not coexist with the extracted .txt (the
    # disk pre-flight only budgets for one copy).
    zip_path.unlink(missing_ok=True)

    if features:
        feat_zip = dest / _FEATURES_REMOTE
        _download_file(base_url + _FEATURES_REMOTE, feat_zip, resume=resume, progress=progress)
        with zipfile.ZipFile(feat_zip) as zf:
            zf.extract("allCountries.txt", dest)
        feat_zip.unlink(missing_ok=True)

    return dest
