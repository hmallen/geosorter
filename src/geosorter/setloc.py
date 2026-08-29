"""Manual location assignment for no-GPS (quarantined) media.

When ``organize`` cannot resolve a coordinate, a capture is quarantined: filed to
``library/_no-gps/<date>/<original_name>`` with a ``files`` row of
``status='quarantined'`` and NULL geo/time columns. This module promotes such a
capture (or many, in one call) to ``organized`` once the user assigns a coordinate
by clicking the map or searching a place name.

Because the ``files`` table never stored the raw capture timestamp (only the
GPS-derived ``capture_ts_*`` columns, all NULL for a no-GPS file), the capture time
is **re-extracted** from the on-disk file at assign time — one ExifTool daemon for
the whole bulk call. The new local date/time is resolved from that raw timestamp
against the assigned coordinate's timezone (an undated file falls back to the file
mtime so it still leaves quarantine).

Each capture is re-filed from ``_no-gps/…`` to ``<place>/<date>/`` with the same
crash-safe, group-atomic **copy -> verify -> os.replace -> delete-last** discipline
as :mod:`geosorter.retag` (the helpers are shared) — so a single coordinate fans out
to one date folder per capture. Provenance becomes ``gps_source='manual'`` and the
row flips to ``status='organized'``. Unknown / already-organized ids are skipped and
reported, never an error.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import config, db, derived, geocoder, pathing, tz_resolver
from .metadata import MetadataExtractor
from .organize import _companion_dest, _strip
from .retag import _RetagError, _cleanup, _relocate, _resolve_collision


@dataclass
class AssignReport:
    """Outcome of one :func:`assign_locations` call."""

    assigned: int = 0          # captures promoted quarantine -> organized
    skipped: int = 0           # unknown ids + ids that are not quarantined
    moved: int = 0             # files physically relocated (primaries + companions)
    place_string: str | None = None
    failures: list[str] = field(default_factory=list)


def _mtime_local(src: Path, lat: float, lon: float) -> tz_resolver.LocalTime:
    """Fallback local time from the file mtime, treated as local wall-clock.

    For an undated capture (no parseable EXIF/QuickTime timestamp) the on-disk
    mtime is the best available signal. It is formatted as a naive wall-clock string
    and resolved against the assigned coordinate's timezone so the capture still gets
    a ``<place>/<date>/`` home instead of staying quarantined.
    """
    naive = datetime.fromtimestamp(src.stat().st_mtime)
    raw = naive.strftime("%Y:%m:%d %H:%M:%S")
    return tz_resolver.resolve_local_time(lat, lon, raw, "EXIF:DateTimeOriginal")


def assign_locations(
    cfg,
    file_ids,
    lat: float,
    lon: float,
    *,
    progress=None,
    extractor_factory=MetadataExtractor,
) -> AssignReport:
    """Assign ``(lat, lon)`` to each quarantined capture in ``file_ids``.

    Geocodes the coordinate once, then for each id loads the ``status='quarantined'``
    ``files`` row (unknown / non-quarantined ids are skipped + counted), re-extracts
    the raw capture time, resolves the local date/time at the new coordinate (mtime
    fallback when undated), recomputes the destination, and re-files the capture
    (primary + companions) from ``_no-gps/…`` to ``<place>/<date>/`` crash-safely,
    flipping the row to ``organized`` with ``gps_source='manual'``. ``progress`` is a
    per-capture one-arg callback (called once per id processed — NOT once per moved
    file — so a job's progress count tracks captures, mirroring the selection count).
    """
    library = Path(cfg.library_root)
    index = db.connect(cfg.index_db_path)
    db.init_index_schema(index)
    geonames = db.connect(cfg.geonames_db_path, integrity_check=False)
    report = AssignReport()
    try:
        geo = geocoder.reverse_geocode(
            geonames, lat, lon, cache_conn=index,
            feature_proximity_km=cfg.feature_proximity_km,
        )
        report.place_string = geo.place_string

        cache_dir = cfg.cache_dir or config.default_cache_dir()
        proxy_cache_dir = config.resolve_proxy_cache_dir(cfg)
        with extractor_factory() as extractor:
            for fid in file_ids:
                try:
                    report.moved += _assign_one(
                        index, library, geo, lat, lon, fid, extractor, progress, report,
                        cache_dir, proxy_cache_dir,
                    )
                except _RetagError as err:
                    report.failures.append(f"{fid}: {err}")
        return report
    finally:
        geonames.close()
        index.close()


def _assign_one(index, library, geo, lat, lon, fid, extractor, progress, report,
                cache_dir=None, proxy_cache_dir=None) -> int:
    """Promote one quarantined capture; return the count of files physically moved."""
    row = index.execute(
        "SELECT dest_path, filename FROM files WHERE id=? AND status='quarantined'",
        (fid,),
    ).fetchone()
    if row is None:
        report.skipped += 1
        return 0
    old_primary_dest, filename = row
    old_primary = Path(_strip(old_primary_dest))

    if progress is not None:
        progress(f"  {filename}")

    md = extractor.extract(str(old_primary))
    local = tz_resolver.resolve_local_time(
        lat, lon, md.capture_ts_raw, md.capture_ts_source_tag
    )
    if not local.local_date or not local.local_time_hms:
        local = _mtime_local(old_primary, lat, lon)
    if not local.local_date or not local.local_time_hms:
        report.failures.append(f"{fid}: could not resolve a local capture time")
        return 0

    stem, ext = os.path.splitext(filename)  # ORIGINAL stem (quarantine files are unrenamed)
    new_primary_dest = pathing.compute_dest_path(library, geo, local, stem, ext)
    new_primary_dest = _resolve_collision(index, fid, new_primary_dest)

    companions = index.execute(
        "SELECT dest_path FROM file_companions WHERE primary_file_id=? ORDER BY id",
        (fid,),
    ).fetchall()
    pairs = [(old_primary_dest, new_primary_dest)]
    for (old_comp_dest,) in companions:
        new_comp_dest = _companion_dest(
            new_primary_dest, old_primary, Path(_strip(old_comp_dest))
        )
        pairs.append((old_comp_dest, new_comp_dest))

    # Group-atomic copy+verify of every file BEFORE any DB write or old-copy delete.
    # `progress` is NOT forwarded: the assign job's denominator counts captures, and
    # `_assign_one` already ticked once per capture above. Passing it here would tick
    # per physically-moved file (primary + each companion), pushing the job's
    # `processed` past its `total` ("6 of 4").
    moved = _relocate(index, pairs, None)

    # One commit: the primary's files row (geo + provenance + status flip + new path),
    # each companion's path, and every moves row's dest_path. dest_sha256 is unchanged.
    index.execute(
        "UPDATE files SET geonameid=?, place_string=?, lat=?, lon=?, "
        "gps_source='manual', geocode_confidence=?, capture_ts_utc=?, "
        "capture_ts_local=?, local_date=?, tz_ambiguous=?, status='organized', "
        "no_gps_hidden=0, dest_path=?, filename=? WHERE id=?",
        (geo.geonameid, geo.place_string, lat, lon, geo.geocode_confidence,
         local.capture_ts_utc, local.capture_ts_local, local.local_date,
         int(local.tz_ambiguous), new_primary_dest,
         os.path.basename(_strip(new_primary_dest)), fid),
    )
    for old_dest, new_dest in pairs:
        if old_dest != new_dest:
            index.execute(
                "UPDATE moves SET dest_path=? WHERE dest_path=?", (new_dest, old_dest)
            )
    for (old_comp_dest,), (_old, new_comp_dest) in zip(companions, pairs[1:]):
        index.execute(
            "UPDATE file_companions SET dest_path=? WHERE primary_file_id=? AND dest_path=?",
            (new_comp_dest, fid, old_comp_dest),
        )
    index.commit()

    # Old copies deleted last (every new copy verified on disk first -> no data loss).
    for old_dest, new_dest in pairs:
        if old_dest != new_dest:
            _cleanup(_strip(old_dest))

    # Invalidate the derived cache for the NEW library paths
    # (m-fix-stale-derived-cache-thumbnails): a promoted capture now occupies a dest that
    # may have held a different capture's stale poster/thumbnail/proxy. (The OLD path is a
    # `_no-gps/…` quarantine location that is never served derived assets, so it needs no
    # invalidation.)
    if cache_dir is not None:
        for old_dest, new_dest in pairs:
            if old_dest != new_dest:
                derived.invalidate(
                    cache_dir, proxy_cache_dir,
                    pathing.library_rel_key(library, new_dest),
                )

    report.assigned += 1
    return moved
