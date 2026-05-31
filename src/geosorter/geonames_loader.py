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


def _load_cities(conn, path: Path, spatial_index: str) -> int:
    rows = []
    for f in _rows(path):
        # cities500 has 19 columns; guard against short lines.
        if len(f) < 18:
            continue
        rows.append(
            (
                _to_int(f[0]),  # geonameid
                f[1],  # name
                f[2],  # ascii_name
                float(f[4]),  # lat
                float(f[5]),  # lon
                f[6] or None,  # feature_class
                f[7] or None,  # feature_code
                f[8] or None,  # country_code
                f[10] or None,  # admin1_code
                f[11] or None,  # admin2_code
                _to_int(f[14]),  # population
                f[17] or None,  # timezone
            )
        )
    conn.executemany(
        "INSERT OR REPLACE INTO geonames "
        "(geonameid, name, ascii_name, lat, lon, feature_class, feature_code, "
        " country_code, admin1_code, admin2_code, population, timezone) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    if spatial_index == "rtree":
        conn.execute(
            "INSERT OR REPLACE INTO geonames_rtree(id, min_lat, max_lat, min_lon, max_lon) "
            "SELECT geonameid, lat, lat, lon, lon FROM geonames"
        )
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
) -> dict[str, int]:
    """Load GeoNames reference data from ``src_dir`` into the geonames DB.

    Expects ``cities500.txt``, ``admin1CodesASCII.txt``, ``admin2Codes.txt`` and
    ``countryInfo.txt`` in ``src_dir``. Idempotent (``INSERT OR REPLACE``).
    Returns a count per source.
    """
    src = Path(src_dir)
    conn = db.connect(geonames_db, integrity_check=False)
    try:
        db.init_geonames_schema(conn, spatial_index=spatial_index)
        counts = {
            "geonames": _load_cities(conn, src / "cities500.txt", spatial_index),
            "admin1": _load_admin(conn, src / "admin1CodesASCII.txt", "admin1_codes"),
            "admin2": _load_admin(conn, src / "admin2Codes.txt", "admin2_codes"),
            "countries": _load_countries(conn, src / "countryInfo.txt"),
        }
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
) -> Path:
    """Download the GeoNames source files into ``dest_dir`` and extract cities500.

    Performs a disk-space pre-flight, downloads each file (resumable), and
    unzips ``cities500.zip`` to ``cities500.txt``. Returns ``dest_dir`` (which
    can then be passed to :func:`load`).
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    free = shutil.disk_usage(dest).free
    if free < min_free_mb * 1024 * 1024:
        raise OSError(
            f"insufficient disk space at {dest}: "
            f"{free // (1024 * 1024)} MiB free, need >= {min_free_mb} MiB"
        )

    for fname in _REMOTE_FILES.values():
        _download_file(base_url + fname, dest / fname, resume=resume, progress=progress)

    zip_path = dest / "cities500.zip"
    with zipfile.ZipFile(zip_path) as zf:
        zf.extract("cities500.txt", dest)
    # Drop the archive so it does not coexist with the extracted .txt (the
    # disk pre-flight only budgets for one copy).
    zip_path.unlink(missing_ok=True)

    return dest
