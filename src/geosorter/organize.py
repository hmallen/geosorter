r"""The ``organize`` pipeline: scan an inbox and file each capture into the library.

This is the integration layer that wires B2 (metadata extraction) and B3
(geocode / tz / path / companion grouping) into the crash-safe
:mod:`geosorter.move_engine`, plus a quarantine router for no-GPS files, a codec
tally, and a duplicate/collision policy. :func:`run_organize` is pure orchestration
returning a :class:`BatchReport`; the click layer (``cli.py``) renders it.

**Group-atomic moves.** A capture (primary + its `.DNG`/`.LRF`/`.SRT`/`_N`
companions) is moved as a unit: every file is copied-and-verified first, and only
if all succeed are the sources deleted — companions first, the primary last. So a
``source_deleted`` row for the primary is a reliable "this whole group is done"
sentinel, and a crash anywhere mid-group recovers on re-run without double-copy or
double-delete (each file is independently idempotent via the move engine).

**Duplicates & collisions** (decision: dedup-then-suffix). A source whose content
hash already exists in ``files`` (from a *different* source — a re-import) is
skipped and left in the inbox. A *different-content* file that resolves to an
already-occupied ``dest_path`` is disambiguated with a ``_2``/``_3`` suffix.
"""

from __future__ import annotations

import os
import secrets
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import db, geocoder, grouping, move_engine, pathing, tz_resolver
from .metadata import MetadataExtractor

# Disk-space safety margin over the raw source bytes (mirrors geonames_loader).
_DISK_MARGIN_MB = 200


@dataclass
class BatchReport:
    """Outcome of one ``organize`` run (or dry-run preview)."""

    batch_id: str
    organized: int = 0
    quarantined: int = 0
    duplicates_skipped: int = 0
    companions: int = 0
    failures: list[str] = field(default_factory=list)
    per_place: dict[str, int] = field(default_factory=dict)
    codec: dict[str, int] = field(default_factory=lambda: {"h264": 0, "h265": 0, "unknown": 0})
    tz_ambiguous: int = 0
    aborted: bool = False
    cancelled: bool = False  # True when a caller-supplied cancel hook halted the run
    dry_run: bool = False
    confirmed: bool = True  # False when the first-run gate was declined


@dataclass
class VerifyReport:
    """Outcome of ``verify-library``."""

    checked: int = 0
    ok: int = 0
    mismatched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def make_batch_id(now: datetime, rand_hex: str) -> str:
    """``YYYYMMDDTHHMMSS-<rand_hex>`` — links files/moves/codec_stats for one run."""
    return now.strftime("%Y%m%dT%H%M%S") + "-" + rand_hex


def is_first_run(conn) -> bool:
    """True when no ``moves`` rows exist yet (a fresh inbox/library)."""
    return conn.execute("SELECT NOT EXISTS(SELECT 1 FROM moves)").fetchone()[0] == 1


def _strip(dest_path: str) -> str:
    return dest_path[4:] if dest_path.startswith("\\\\?\\") else dest_path


def _quarantine_date(md, primary: Path) -> str:
    """Date folder for a quarantined file: capture-ts date if parseable, else mtime."""
    if md.capture_ts_raw:
        parsed = tz_resolver._parse_naive(md.capture_ts_raw)
        if parsed is not None:
            return parsed.strftime("%Y-%m-%d")
    return datetime.fromtimestamp(primary.stat().st_mtime).strftime("%Y-%m-%d")


def _companion_dest(primary_dest: str, primary_src: Path, companion_src: Path) -> str:
    r"""Destination for a companion: the primary's folder, named off the primary.

    The companion adopts the primary's (possibly suffix-resolved) final stem plus
    any extra suffix from its own original name — so a split-video ``_001`` segment
    keeps its distinguishing tail, and a collision-suffixed primary carries its
    companions to unique names too (in both the organize and quarantine branches).
    The primary's ``\\?\`` long-path prefix, if any, is preserved.
    """
    prefix = "\\\\?\\" if primary_dest.startswith("\\\\?\\") else ""
    final = _strip(primary_dest)
    folder = os.path.dirname(final)
    primary_stem = Path(final).stem
    if companion_src.stem.upper().startswith(primary_src.stem.upper()):
        extra = companion_src.stem[len(primary_src.stem):]
    else:
        extra = "_" + companion_src.stem
    return prefix + os.path.join(folder, primary_stem + extra + companion_src.suffix)


def _is_duplicate(conn, primary: Path, src_sha: str) -> bool:
    """True if identical content was already organized from a *different* source."""
    if conn.execute("SELECT 1 FROM files WHERE sha256=? LIMIT 1", (src_sha,)).fetchone() is None:
        return False
    mine = conn.execute(
        "SELECT 1 FROM moves WHERE source_path=? AND status IN ('copy_verified','source_deleted') LIMIT 1",
        (str(primary),),
    ).fetchone()
    return mine is None


def _resolve_collision(conn, primary: Path, dest_path: str) -> str:
    """Suffix the path if a *different* source already occupies it; else unchanged."""
    row = conn.execute(
        "SELECT m.source_path FROM files f LEFT JOIN moves m ON m.file_id = f.id "
        "WHERE f.dest_path=? LIMIT 1",
        (dest_path,),
    ).fetchone()
    if row is None or row[0] == str(primary):
        return dest_path
    final = _strip(dest_path)
    prefix = dest_path[: len(dest_path) - len(final)]
    stem, ext = os.path.splitext(final)
    n = 2
    while True:
        candidate = f"{prefix}{stem}_{n}{ext}"
        taken_in_db = conn.execute(
            "SELECT 1 FROM files WHERE dest_path=? LIMIT 1", (candidate,)
        ).fetchone()
        if taken_in_db is None and not os.path.exists(f"{stem}_{n}{ext}"):
            return candidate
        n += 1


def _disk_preflight(paths: list[Path], library: Path) -> None:
    """Raise ``OSError`` if the library volume lacks room for the source bytes."""
    need = sum(p.stat().st_size for p in paths)
    library.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(library).free
    if free < need + _DISK_MARGIN_MB * 1024 * 1024:
        raise OSError(
            f"insufficient disk space at {library}: {free // (1024 * 1024)} MiB free, "
            f"need {need // (1024 * 1024)} MiB + {_DISK_MARGIN_MB} MiB margin"
        )


def _preview(groups, inbox: Path, library: Path) -> str:
    n = sum(1 + len(g.companions) for g in groups)
    return (
        f"First run on a new library.\n"
        f"  inbox:   {inbox}\n"
        f"  library: {library}\n"
        f"  {len(groups)} captures ({n} files) will be COPIED then their sources DELETED."
    )


def run_organize(
    cfg,
    *,
    dry_run: bool = False,
    assume_yes: bool = False,
    confirm=None,
    progress=None,
    cancel=None,
    extractor_factory=MetadataExtractor,
) -> BatchReport:
    """Scan ``cfg.inbox_path`` and organize every capture into ``cfg.library_root``.

    ``dry_run`` performs zero filesystem and zero DB writes but returns the same
    counts. ``confirm`` is called once (with a preview string) on the first
    destructive run unless ``assume_yes``; returning False aborts with no writes.
    ``cancel``, if given, is a no-arg predicate polled **between** capture groups
    (never mid-group, so group-atomicity holds); when it returns True the run stops
    and ``report.cancelled`` is set, leaving unprocessed captures in the inbox.
    ``extractor_factory`` is injectable for tests.
    """
    if cfg.inbox_path is None or cfg.library_root is None:
        raise ValueError("organize requires both inbox_path and library_root in geosorter.toml")
    inbox = Path(cfg.inbox_path)
    library = Path(cfg.library_root)
    if not inbox.exists():
        raise ValueError(f"inbox_path does not exist: {inbox}")

    index = db.connect(cfg.index_db_path)
    db.init_index_schema(index)
    geonames = db.connect(cfg.geonames_db_path, integrity_check=False)
    try:
        paths = [p for p in sorted(inbox.rglob("*")) if p.is_file()]
        groups = grouping.group_companions(paths)
        report = BatchReport(batch_id="(dry-run)", dry_run=dry_run)

        if not dry_run and not assume_yes and is_first_run(index):
            if confirm is not None and not confirm(_preview(groups, inbox, library)):
                report.confirmed = False
                return report

        if not dry_run:
            _disk_preflight(paths, library)
            report.batch_id = make_batch_id(datetime.now(), secrets.token_hex(3))

        with extractor_factory() as extractor:
            for group in groups:
                if report.aborted:
                    break
                if cancel is not None and cancel():
                    report.cancelled = True
                    break
                _process_group(group, extractor, index, geonames, library, report,
                               dry_run, progress, cfg.feature_proximity_km)

        if not dry_run:
            index.execute(
                "INSERT INTO codec_stats(batch_id, h264_count, h265_count, unknown_count) "
                "VALUES (?,?,?,?)",
                (report.batch_id, report.codec["h264"], report.codec["h265"], report.codec["unknown"]),
            )
            index.commit()
        return report
    finally:
        geonames.close()
        index.close()


def _process_group(group, extractor, index, geonames, library, report, dry_run,
                   progress, feature_proximity_km=5.0) -> None:
    primary = group.primary

    # Group fully done in a prior run (primary is deleted last → reliable sentinel).
    if not dry_run and move_engine.is_already_moved(index, primary):
        return

    md = extractor.extract(primary)
    if md.media_type == "video":  # codec stats are a video-only tally (HEVC decision)
        report.codec[md.codec if md.codec in ("h264", "h265") else "unknown"] += 1
    if progress is not None:
        progress(f"  {primary.name}")

    local = tz_resolver.resolve_local_time(
        md.lat, md.lon, md.capture_ts_raw, md.capture_ts_source_tag
    )
    quarantine = md.lat is None or md.lon is None or local.local_date is None

    geo = None
    if quarantine:
        qdate = pathing.sanitize_component(_quarantine_date(md, primary), fallback="undated")
        primary_dest = os.path.join(str(library), "_no-gps", qdate, primary.name)
    else:
        geo = geocoder.reverse_geocode(
            geonames, md.lat, md.lon, cache_conn=(None if dry_run else index),
            feature_proximity_km=feature_proximity_km,
        )
        primary_dest = pathing.compute_dest_path(library, geo, local, primary.stem, primary.suffix)

    if dry_run:
        _tally(report, group, geo, quarantine, local)
        return

    src_sha = move_engine.sha256_file(primary)
    if _is_duplicate(index, primary, src_sha):
        report.duplicates_skipped += 1
        return
    primary_dest = _resolve_collision(index, primary, primary_dest)

    files_to_move = [(primary, primary_dest)]
    for cpath, ctype in group.companions:
        files_to_move.append((cpath, _companion_dest(primary_dest, primary, cpath)))

    # Phase A: copy + verify every file in the group (no deletes yet). Record each
    # file's verified source hash so Phase B's delete keys on the exact moves row.
    shas: dict[Path, str] = {}
    for sp, dp in files_to_move:
        if move_engine.is_already_moved(index, sp):
            continue
        outcome = move_engine.copy_and_verify(index, report.batch_id, sp, dp)
        if outcome.status == "failed":
            report.aborted = True
            report.failures.append(f"{sp}: {outcome.error}")
            return  # group-atomic: delete NO source in this group, halt the batch
        shas[sp] = outcome.source_sha256

    # Phase B: persist the index rows, then delete sources — companions first, the
    # primary last, so the primary's source_deleted row is a reliable group-done
    # sentinel. Delete keys on the stored hash, never a re-hash of the live file.
    primary_sha = shas.get(primary) or _stored_sha(index, primary)
    _persist(index, report, md, geo, local, quarantine, primary, primary_dest, group, files_to_move, primary_sha)
    for sp, _dp in reversed(files_to_move):
        sha = shas.get(sp)
        if sha is not None:  # already-deleted files (skipped in Phase A) need nothing
            move_engine.commit_delete(index, sp, sha)

    _tally(report, group, geo, quarantine, local)


def _persist(index, report, md, geo, local, quarantine, primary, primary_dest, group, files_to_move, primary_sha) -> None:
    """Insert/refresh the ``files`` row and its ``file_companions`` (idempotent)."""
    row = index.execute("SELECT id FROM files WHERE dest_path=?", (primary_dest,)).fetchone()
    if row is not None:
        file_id = row[0]
    else:
        cur = index.execute(
            "INSERT INTO files(geonameid, place_string, dest_path, filename, media_type, "
            "capture_ts_utc, capture_ts_local, local_date, lat, lon, gps_source, "
            "geocode_confidence, tz_ambiguous, codec, width, height, duration_s, sha256, "
            "status, batch_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                geo.geonameid if geo else None,
                geo.place_string if geo else None,
                primary_dest,
                os.path.basename(_strip(primary_dest)),
                md.media_type,
                local.capture_ts_utc,
                local.capture_ts_local,
                local.local_date,
                md.lat,
                md.lon,
                md.gps_source,
                geo.geocode_confidence if geo else None,
                int(local.tz_ambiguous),
                md.codec,
                md.width,
                md.height,
                md.duration_s,
                primary_sha,
                "quarantined" if quarantine else "organized",
                report.batch_id,
            ),
        )
        file_id = cur.lastrowid

    # Link the primary's moves row and (re)write companion rows idempotently.
    index.execute(
        "UPDATE moves SET file_id=? WHERE source_path=? AND file_id IS NULL",
        (file_id, str(primary)),
    )
    index.execute("DELETE FROM file_companions WHERE primary_file_id=?", (file_id,))
    for (cpath, ctype), (_sp, cdest) in zip(group.companions, files_to_move[1:]):
        index.execute(
            "INSERT INTO file_companions(primary_file_id, dest_path, companion_type) VALUES (?,?,?)",
            (file_id, cdest, ctype),
        )
    index.commit()


def _stored_sha(index, primary: Path) -> str:
    """The verified source hash from the primary's moves row (fallback on recovery)."""
    row = index.execute(
        "SELECT source_sha256 FROM moves WHERE source_path=? "
        "AND status IN ('copy_verified','source_deleted') LIMIT 1",
        (str(primary),),
    ).fetchone()
    return row[0] if row else move_engine.sha256_file(primary)


def _tally(report, group, geo, quarantine, local) -> None:
    if quarantine:
        report.quarantined += 1
    else:
        report.organized += 1
        place = (geo.place_string if geo and geo.place_string else "_unknown")
        report.per_place[place] = report.per_place.get(place, 0) + 1
    report.companions += len(group.companions)
    if local.tz_ambiguous:
        report.tz_ambiguous += 1


def verify_library(cfg) -> VerifyReport:
    """Recompute on-disk hashes of organized files vs the verified ``moves.dest_sha256``."""
    index = db.connect(cfg.index_db_path)
    db.init_index_schema(index)  # tolerate a never-organized library (empty moves)
    report = VerifyReport()
    try:
        rows = index.execute(
            "SELECT dest_path, dest_sha256 FROM moves "
            "WHERE status IN ('copy_verified','source_deleted') AND dest_sha256 IS NOT NULL"
        ).fetchall()
        for dest_path, dest_sha in rows:
            report.checked += 1
            on_disk = _strip(dest_path)
            if not os.path.exists(on_disk):
                report.missing.append(on_disk)
                continue
            if move_engine.sha256_file(on_disk) == dest_sha:
                report.ok += 1
            else:
                report.mismatched.append(on_disk)
        return report
    finally:
        index.close()
