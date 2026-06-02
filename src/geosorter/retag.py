r"""Manual map-click re-tag (Phase 2 / B8): re-file an organized capture to new GPS.

The user clicks the map to correct (or assign) an already-organized capture's
location. ``retag_file`` re-geocodes the clicked coordinate, recomputes the
destination from the file's **stored** ``capture_ts_utc`` against the new
timezone, and moves the capture (primary + companions) to the new place/date
folder with the same crash-safe **copy -> verify -> replace -> delete** discipline
as :mod:`geosorter.move_engine` / :mod:`geosorter.undo` (never ``os.rename`` --
``library_root`` may be a different volume). The ``files`` / ``file_companions`` /
``moves`` rows are updated in place (``dest_sha256`` is unchanged -- the bytes do
not change), and the provenance becomes ``gps_source='manual'``.

The move is **group-atomic** (every file is copied + verified before any old copy
is deleted) and idempotent within a call by disk state: a verified copy always
exists at the new path before the old is removed, so a crash never loses data and
never clobbers unrelated content.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import db, geocoder, move_engine, pathing, tz_resolver
from .organize import _companion_dest, _strip


@dataclass
class RetagReport:
    """Outcome of one :func:`retag_file` call."""

    file_id: int | None = None
    status: str = ""  # 'retagged' | 'not_found' | 'failed'
    old_dest: str | None = None
    new_dest: str | None = None
    place_string: str | None = None
    moved: int = 0  # files physically relocated (0 when the path is unchanged)
    error: str | None = None


class _RetagError(Exception):
    """Internal: a re-tag move could not be completed safely."""


def _dji_orig(filename: str) -> str:
    """Recover the original DJI stem from a stored ``<date>_<time>_<orig><ext>`` name.

    The date (``YYYY-MM-DD``) and time (``HH-MM-SS``) tokens contain no ``_``, so
    splitting the stem on ``_`` with ``maxsplit=2`` isolates the original stem.
    """
    stem = os.path.splitext(filename)[0]
    parts = stem.split("_", 2)
    return parts[2] if len(parts) == 3 else stem


def _resolve_collision(index, file_id: int, dest_path: str) -> str:
    """Suffix ``dest_path`` with ``_2``/``_3``/... if a *different* file already holds it.

    ``files.dest_path`` is ``UNIQUE``; re-filing onto a path another organized capture
    occupies would violate the constraint (and, worse, only after the bytes were
    copied). Mirrors :func:`geosorter.organize._resolve_collision`, keyed on the file
    id (our own row, still at the old path, never matches the new path). Disk presence
    is intentionally *not* consulted here — a new path already holding our own verified
    bytes is a legitimate resume that :func:`_relocate` accepts.
    """
    final = _strip(dest_path)
    prefix = dest_path[: len(dest_path) - len(final)]
    stem, ext = os.path.splitext(final)
    candidate = dest_path
    n = 2
    while True:
        row = index.execute(
            "SELECT id FROM files WHERE dest_path=? LIMIT 1", (candidate,)
        ).fetchone()
        if row is None or row[0] == file_id:
            return candidate
        candidate = f"{prefix}{stem}_{n}{ext}"
        n += 1


def _stored_sha(index, dest_path: str) -> str | None:
    row = index.execute(
        "SELECT dest_sha256 FROM moves WHERE dest_path=? AND dest_sha256 IS NOT NULL LIMIT 1",
        (dest_path,),
    ).fetchone()
    return row[0] if row else None


def retag_file(cfg, file_id: int, lat: float, lon: float, *, progress=None) -> RetagReport:
    """Re-file the organized capture ``file_id`` to the place implied by ``(lat, lon)``.

    Returns a :class:`RetagReport`. ``status`` is ``'not_found'`` when the id is
    unknown or not ``organized``, ``'failed'`` on a verify/IO error or an
    unresolvable timezone (no rows changed), else ``'retagged'``. ``progress`` is a
    per-file one-arg callback (mirrors :func:`geosorter.organize.run_organize`).

    "No move needed" is decided by the recomputed destination equalling the current
    one, not by coordinate equality: re-geocoding the same coordinate can yield a
    different destination if the geonames data or thresholds changed since organize —
    that is intended (the click re-runs the current geocoder), just noted here.
    """
    library = Path(cfg.library_root)
    index = db.connect(cfg.index_db_path)
    db.init_index_schema(index)
    geonames = db.connect(cfg.geonames_db_path, integrity_check=False)
    try:
        row = index.execute(
            "SELECT dest_path, filename, capture_ts_utc FROM files "
            "WHERE id=? AND status='organized'",
            (file_id,),
        ).fetchone()
        if row is None:
            return RetagReport(file_id=file_id, status="not_found")
        old_primary_dest, filename, capture_ts_utc = row
        report = RetagReport(file_id=file_id, old_dest=old_primary_dest)

        geo = geocoder.reverse_geocode(
            geonames, lat, lon, cache_conn=index,
            feature_proximity_km=cfg.feature_proximity_km,
        )
        local = tz_resolver.local_time_from_utc(lat, lon, capture_ts_utc)
        if not local.local_date or not local.local_time_hms:
            report.status = "failed"
            report.error = "could not resolve a local time for the new location"
            return report

        ext = os.path.splitext(filename)[1]
        new_primary_dest = pathing.compute_dest_path(
            library, geo, local, _dji_orig(filename), ext
        )
        # Disambiguate against a different organized capture already at that path
        # (files.dest_path is UNIQUE) before computing companion paths off it.
        new_primary_dest = _resolve_collision(index, file_id, new_primary_dest)
        report.new_dest = new_primary_dest
        report.place_string = geo.place_string

        # Old + new path for the primary and every companion. _companion_dest
        # derives each companion's new name from the new primary name using the
        # same stem-prefix rule organize used on the way in.
        old_primary = Path(_strip(old_primary_dest))
        pairs = [(old_primary_dest, new_primary_dest)]
        companions = index.execute(
            "SELECT dest_path FROM file_companions WHERE primary_file_id=? ORDER BY id",
            (file_id,),
        ).fetchall()
        for (old_comp_dest,) in companions:
            new_comp_dest = _companion_dest(
                new_primary_dest, old_primary, Path(_strip(old_comp_dest))
            )
            pairs.append((old_comp_dest, new_comp_dest))

        try:
            moved = _relocate(index, pairs, progress)
        except _RetagError as err:
            report.status = "failed"
            report.error = str(err)
            return report

        # Update the index in one commit: the primary's files row (geo + provenance
        # + new path), each companion's path, and every moves row's dest_path. The
        # dest_sha256 stays as-is because the content is unchanged.
        index.execute(
            "UPDATE files SET geonameid=?, place_string=?, lat=?, lon=?, "
            "gps_source='manual', capture_ts_local=?, local_date=?, dest_path=?, "
            "filename=? WHERE id=?",
            (geo.geonameid, geo.place_string, lat, lon,
             local.capture_ts_local, local.local_date, new_primary_dest,
             os.path.basename(_strip(new_primary_dest)), file_id),
        )
        for old_dest, new_dest in pairs:
            if old_dest != new_dest:
                index.execute(
                    "UPDATE moves SET dest_path=? WHERE dest_path=?", (new_dest, old_dest)
                )
        for (old_comp_dest,), (_old, new_comp_dest) in zip(companions, pairs[1:]):
            index.execute(
                "UPDATE file_companions SET dest_path=? WHERE primary_file_id=? AND dest_path=?",
                (new_comp_dest, file_id, old_comp_dest),
            )
        index.commit()

        # Group-atomic delete: every new copy is verified on disk, so removing the
        # old copies last is safe (a crash here leaves harmless orphans, never loss).
        for old_dest, new_dest in pairs:
            if old_dest != new_dest:
                _cleanup(_strip(old_dest))

        report.moved = moved
        report.status = "retagged"
        return report
    finally:
        geonames.close()
        index.close()


def _relocate(index, pairs, progress) -> int:
    """Copy+verify every changed file to its new path (old copies left in place).

    Returns the number of files physically copied. Idempotent by disk state: a new
    path already present (with the verified content) is accepted as done. Raises
    :class:`_RetagError` on a verify mismatch, a missing source, or a new path
    occupied by *different* content (never clobbered).
    """
    moved = 0
    for old_dest, new_dest in pairs:
        if old_dest == new_dest:
            continue
        old_s, new_s = _strip(old_dest), _strip(new_dest)
        if progress is not None:
            progress(f"  {os.path.basename(new_s)}")
        sha = _stored_sha(index, old_dest) or move_engine.sha256_file(old_s)

        if os.path.exists(new_s):
            # Resume / our-own-copy: accept only when the bytes match the expected
            # hash; otherwise the new path holds unrelated content -> never clobber.
            if move_engine.sha256_file(new_s) != sha:
                raise _RetagError(f"{new_s}: destination occupied by different content")
            continue
        if not os.path.exists(old_s):
            raise _RetagError(f"{old_s}: source file missing")

        partial = new_s + ".partial"
        try:
            os.makedirs(os.path.dirname(new_s), exist_ok=True)
            shutil.copyfile(old_s, partial)
        except OSError as err:
            _cleanup(partial)
            raise _RetagError(f"{new_s}: {err}") from err
        if move_engine.sha256_file(partial) != sha:
            _cleanup(partial)
            raise _RetagError(f"{new_s}: verify mismatch after copy")
        os.replace(partial, new_s)
        moved += 1
    return moved


def _cleanup(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
