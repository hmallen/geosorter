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
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

from . import db, geocoder, grouping, inference, misc_parser, move_engine, pathing, tz_resolver
from .metadata import MediaMetadata, MetadataExtractor

# Files whose extension implies a video when ExifTool cannot read the metadata.
_VIDEO_EXTS = {".mp4", ".mov"}

# Bytes moved between two mid-run free-space rechecks. A recheck is one
# ``shutil.disk_usage`` call (a network round trip over SMB), so it is throttled to
# roughly once per gigabyte rather than once per group at scale.
_FREESPACE_RECHECK_BYTES = 1 << 30  # 1 GiB


@dataclass
class BatchReport:
    """Outcome of one ``organize`` run (or dry-run preview)."""

    batch_id: str
    organized: int = 0
    inferred: int = 0  # subset of `organized` whose location was borrowed (B8)
    quarantined: int = 0
    duplicates_skipped: int = 0
    duplicates_relocated: int = 0  # duplicates moved to <inbox>/_duplicates/ (relocate_duplicates)
    companions: int = 0
    retained_frame_bytes: int = 0  # disk cost of retained hyperlapse frames (B10)
    unclaimed: int = 0  # recognized PANORAMA/MISC paths B10 does not file (B10)
    ratings_applied: int = 0  # files given a star_rating from a MISC catalog (B11)
    warnings: list[str] = field(default_factory=list)  # orphan frame dirs etc. (B10)
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


def _frame_dest(primary_dest: str, primary_src: Path, frame_src: Path) -> str:
    r"""Destination for a frame companion: a ``<primary_stem>_frames/`` subfolder.

    Shared by hyperlapse frames (``HYPERLAPSE_0001.JPG``) and panorama tiles
    (``PANO_0002.JPG``). The frame keeps its original DJI name — already unique and
    ordered within the source dir — inside a subfolder beside the primary named off
    the primary's (possibly suffix-resolved) final stem. The primary's ``\\?\``
    long-path prefix, if any, is preserved. ``move_engine`` creates the subfolder.
    """
    prefix = "\\\\?\\" if primary_dest.startswith("\\\\?\\") else ""
    final = _strip(primary_dest)
    folder = os.path.dirname(final)
    primary_stem = Path(final).stem
    return prefix + os.path.join(folder, primary_stem + "_frames", frame_src.name)


def _borrow_frame_gps(group, md, extractor):
    """Return ``md`` with GPS borrowed from the first GPS-bearing frame (B10).

    Frames are pre-sorted by name, so this picks the earliest frame that carries a
    coordinate. If no frame has GPS, ``md`` is returned unchanged (the render then
    falls back to neighbor-GPS inference / quarantine like any other no-GPS file).
    """
    for fpath, ctype in group.companions:
        if ctype != "hyperlapse_frame":
            continue
        fmd = extractor.extract(fpath)
        if fmd.lat is not None and fmd.lon is not None:
            return replace(md, lat=fmd.lat, lon=fmd.lon, gps_source="hyperlapse_frame")
    return md


def _is_duplicate(conn, primary: Path, src_sha: str) -> bool:
    """True if identical content was already organized from a *different* source.

    Gated first on a ``files.sha256`` match. The "mine" check is then: does THIS
    ``(source_path, src_sha)`` pair have a ``moves`` row of any status? Such a row — be
    it ``pending``/``failed`` (an in-flight or failed attempt on the same-volume rename
    path, where the ``files`` + ``moves`` rows are written before the source is renamed,
    keyed on the current hash) or ``copy_verified``/``source_deleted`` — is our own move
    of this exact source+content, not a foreign re-import. So a resumed/retried run never
    skips its own source as a duplicate. The match is keyed on the **current** hash, so a
    genuine duplicate (a different ``source_path``, hence no row) AND a path carrying only
    a STALE row from an older, different-bytes attempt are both still correctly skipped.
    """
    if conn.execute("SELECT 1 FROM files WHERE sha256=? LIMIT 1", (src_sha,)).fetchone() is None:
        return False
    mine = conn.execute(
        "SELECT 1 FROM moves WHERE source_path=? AND source_sha256=? LIMIT 1",
        (str(primary), src_sha),
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


def _relocate_duplicate(index, group, companions, inbox, src_sha: str, report) -> None:
    r"""Move a duplicate capture out of the inbox into ``<inbox>/_duplicates/`` + log it.

    A duplicate is byte-identical content already verified in ``library_root``, so this is
    a plain move (no copy-verify, never touches the library). The whole capture (primary +
    its effective companions) moves together, each preserving its inbox-relative subpath so
    recycled DJI names across cards never collide, and one TSV line — incoming primary path
    and the matched library ``dest_path`` — is appended to ``_duplicates/duplicates.log``.
    """
    dup_root = Path(inbox) / grouping.DUPLICATES_DIRNAME
    # Companions FIRST, primary LAST — mirroring the organizer's move discipline. This
    # path is off the crash-safe move log, so if a move fails midway the primary must
    # still be in the inbox for the next run to rebuild and retry the group from it
    # (a primary moved out first would strand any unmoved companion as an orphan).
    for src in [c for c, _t in companions] + [group.primary]:
        if not src.exists():
            continue
        target = _dup_target(dup_root, Path(inbox), src)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(src, target)  # atomic within the inbox volume
        except OSError:
            try:
                shutil.move(str(src), str(target))  # cross-device fallback
            except OSError as exc:
                # A duplicate is already safe in the library, so a relocation failure is
                # non-fatal: record it and leave the rest in place rather than aborting
                # the whole run. The next run rebuilds the group from the primary.
                report.failures.append(f"{src}: duplicate relocation failed: {exc}")
                return
    row = index.execute(
        "SELECT dest_path FROM files WHERE sha256=? LIMIT 1", (src_sha,)
    ).fetchone()
    matched_dest = _strip(row[0]) if row else ""
    dup_root.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now().isoformat(timespec='seconds')}\t{report.batch_id}\t{group.primary}\t{matched_dest}\n"
    with open(dup_root / "duplicates.log", "a", encoding="utf-8") as fh:
        fh.write(line)
    report.duplicates_relocated += 1


def _dup_target(dup_root: Path, inbox: Path, src: Path) -> Path:
    """Non-clobbering ``_duplicates/`` destination preserving ``src``'s inbox subpath."""
    target = dup_root / src.relative_to(inbox)
    if not target.exists():
        return target
    stem, ext = os.path.splitext(target.name)
    n = 2
    while True:
        candidate = target.with_name(f"{stem}_{n}{ext}")
        if not candidate.exists():
            return candidate
        n += 1


def _same_volume(a: Path, b: Path) -> bool:
    """True when ``a`` and ``b`` live on the same filesystem volume.

    Compares ``os.stat().st_dev`` (on Windows the volume serial), so an inbox and a
    library that are two folders on one SMB share / one local disk are recognized as
    same-volume. A move within one volume can be an atomic, server-side ``os.replace``
    rename (zero bytes over the wire) instead of a copy+verify+delete. Mis-detection
    is always safe: a false negative just keeps the (correct, slower) copy path, and a
    false positive surfaces as an ``EXDEV`` failure in ``move_engine.rename_in_place``
    with the source left in place. Both paths must already exist (callers stat the
    inbox and the just-created library root). Any ``OSError`` -> ``False`` (fall back
    to copy). ``st_dev == 0`` (unknown) is treated as not-same-volume.
    """
    try:
        dev_a = a.stat().st_dev
        return dev_a != 0 and dev_a == b.stat().st_dev
    except OSError:
        return False


def _disk_preflight(total_bytes: int, library: Path, margin_bytes: int) -> None:
    """Raise ``OSError`` if the library volume lacks room for the source bytes.

    ``total_bytes`` is the size of the files actually being moved this run (the
    selection-filtered capture groups, primary + companions), and ``margin_bytes``
    is the configurable headroom kept free on top of them.
    """
    library.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(library).free
    if free < total_bytes + margin_bytes:
        mib = 1024 * 1024
        raise OSError(
            f"insufficient disk space at {library}: {free // mib} MiB free, "
            f"need {total_bytes // mib} MiB + {margin_bytes // mib} MiB margin"
        )


def _sweep_stale_partials(index) -> None:
    """Delete ``.partial`` files left by a crashed prior run (reclaim the share).

    Every ``.partial`` corresponds to a committed ``moves`` row in ``status='pending'``
    (the row is inserted and committed before the copy begins), so the orphans can be
    found from the index DB — no full-library walk, which would be brutal over SMB at
    20k files. The pending rows are left intact: a re-processed group resumes through
    the move engine, which overwrites its partial anyway.
    """
    for (dest_path,) in index.execute(
        "SELECT dest_path FROM moves WHERE status='pending'"
    ).fetchall():
        move_engine._cleanup(_strip(dest_path) + ".partial")


def _finalize_renamed_pending(index) -> None:
    """Finalize ``pending`` rows left by a same-volume rename that crashed mid-flip.

    On the rename path the source is removed atomically by ``os.replace``; if the
    process dies in the sub-millisecond window between that and the
    ``status='source_deleted'`` commit, the row is left ``pending`` with the source
    gone and the destination present. Because resume rebuilds capture groups from the
    *inbox* (where the source no longer is), that row would never be reconciled — so the
    file, though already filed (its ``files`` row was persisted first), would keep a
    ``pending`` move row that ``undo`` mishandles. This sweep reconciles such rows from
    the move log directly (mirroring :func:`_sweep_stale_partials`): a ``pending`` row
    whose source is GONE and whose destination EXISTS is flipped to ``source_deleted``
    with ``dest_sha256`` = the stored source hash (the bytes are identical — a rename
    copied nothing). It never touches a pending COPY row, whose source is still present.
    """
    for source_path, dest_path, sha in index.execute(
        "SELECT source_path, dest_path, source_sha256 FROM moves WHERE status='pending'"
    ).fetchall():
        if not os.path.exists(source_path) and os.path.exists(_strip(dest_path)):
            index.execute(
                "UPDATE moves SET status='source_deleted', dest_sha256=?, "
                "completed_at=datetime('now') WHERE source_path=? AND source_sha256=?",
                (sha, source_path, sha),
            )
    index.commit()


def _unextractable_md(path: Path) -> MediaMetadata:
    """Synthetic, no-metadata :class:`MediaMetadata` for an unreadable file.

    Used when ExifTool cannot read a file after every retry: ``media_type`` is
    guessed from the extension and everything else is ``None``/``'none'``, so the
    file routes through the existing no-GPS/no-date quarantine path (filed under
    ``_no-gps/<mtime-date>/``) rather than blocking the pass or being lost.
    """
    media_type = "video" if path.suffix.lower() in _VIDEO_EXTS else "photo"
    return MediaMetadata(
        media_type=media_type, lat=None, lon=None, gps_source="none",
        capture_ts_raw=None, capture_ts_source_tag=None, width=None, height=None,
        duration_s=None, codec=None, codec_raw=None, exif_gps=None, srt_gps=None,
    )


def _close_extractor(extractor) -> None:
    """Terminate an extractor, swallowing errors (it may be a crashed daemon)."""
    try:
        extractor.__exit__(None, None, None)
    except Exception:  # never let cleanup of a dead daemon mask the real outcome
        pass


def _extract_one(group, extractor, extractor_factory, max_failures, report):
    """Extract one group's metadata, restarting a crashed daemon and retrying.

    Returns ``(md, extractor)`` — ``extractor`` may be a fresh instance after a
    restart, so the caller must keep using the returned one. On a per-file error the
    daemon is terminated and a fresh one started (a daemon crash is the failure mode
    this guards against); after ``max_failures`` attempts the file is given a
    synthetic no-metadata md (it will quarantine) and recorded in ``report.failures``.
    """
    attempt = 0
    while True:
        try:
            md = extractor.extract(group.primary)
            # A hyperlapse render carries no GPS of its own; borrow it from the first
            # GPS-bearing frame so the render lands on the map (B10).
            if group.capture_kind == "hyperlapse" and md.lat is None and md.lon is None:
                md = _borrow_frame_gps(group, md, extractor)
            return md, extractor
        except Exception as exc:  # daemon crash, broken pipe, or unreadable file
            attempt += 1
            if attempt >= max_failures:
                report.failures.append(
                    f"{group.primary}: extraction failed after {attempt} attempt(s) "
                    f"({exc}) — quarantined"
                )
                return _unextractable_md(group.primary), extractor
            _close_extractor(extractor)  # restart the (possibly dead) daemon
            # A failure to RE-START the daemon (binary gone, version gate, OS out of
            # handles) is deliberately allowed to propagate and abort the pass rather
            # than being caught: a permanently-broken ExifTool would otherwise silently
            # quarantine every remaining file into _no-gps. Aborting is safer — the
            # sources stay in the inbox and the (idempotent) run resumes once fixed.
            extractor = extractor_factory()
            extractor.__enter__()


def _extract_pass(pending, extractor_factory, chunk_size, max_failures, report):
    """Pass-1 metadata extraction, chunked across fresh ExifTool daemons.

    ``pending`` is ``[(idx, group), ...]`` (the original group index preserved so
    pass-2 inference still keys on it). Each chunk of ``chunk_size`` groups runs under
    its own daemon — bounding daemon memory growth over a 20k-file run and giving a
    natural restart cadence — and any daemon crash inside a chunk restarts and retries
    the offending file (see :func:`_extract_one`), so a crash loses ≤1 file, not the
    whole pass. Returns ``[(idx, group, md), ...]``.
    """
    extracted: list[tuple[int, grouping.CaptureGroup, object]] = []
    for start in range(0, len(pending), chunk_size):
        extractor = extractor_factory()
        extractor.__enter__()
        try:
            for idx, group in pending[start:start + chunk_size]:
                md, extractor = _extract_one(
                    group, extractor, extractor_factory, max_failures, report
                )
                extracted.append((idx, group, md))
        finally:
            _close_extractor(extractor)
    return extracted


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
    byte_progress=None,
    cancel=None,
    selected_primaries: set[str] | None = None,
    extractor_factory=MetadataExtractor,
    on_plan=None,
) -> BatchReport:
    """Scan ``cfg.inbox_path`` and organize every capture into ``cfg.library_root``.

    ``dry_run`` performs zero filesystem and zero DB writes but returns the same
    counts. ``confirm`` is called once (with a preview string) on the first
    destructive run unless ``assume_yes``; returning False aborts with no writes.
    ``cancel``, if given, is a no-arg predicate polled **between** capture groups
    (never mid-group, so group-atomicity holds); when it returns True the run stops
    and ``report.cancelled`` is set, leaving unprocessed captures in the inbox.
    ``byte_progress``, if given, is called as ``byte_progress(filename, phase, done,
    total)`` during each file's copy/hash so a caller can show live within-file
    progress on a slow drive (the CLI omits it; the HTTP job manager uses it).
    ``selected_primaries``, if given, restricts the run to the capture groups whose
    primary's inbox-relative POSIX path is in the set (a partial import from the map
    UI); ``None`` imports every group (the CLI/headless default). A partial import
    also skips the destructive MISC-catalog archive (ratings are still applied to the
    selected files) so a catalog is never archived while unselected media remains.
    ``extractor_factory`` is injectable for tests. ``on_plan``, if given, is called
    once as ``on_plan(total_groups, total_bytes)`` after the selection filter so a
    caller (the HTTP job manager) can show "N of M captures" and a bytes-based ETA.
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
        paths = grouping.scan_inbox_files(inbox)
        pre = grouping.prescan_inbox(paths, inbox_root=inbox)
        groups = pre.groups
        if selected_primaries is not None:
            groups = [
                g
                for g in groups
                if g.primary.relative_to(inbox).as_posix() in selected_primaries
            ]
        report = BatchReport(batch_id="(dry-run)", dry_run=dry_run)
        report.warnings.extend(pre.warnings)
        report.unclaimed = len(pre.unclaimed)

        # Bytes actually being moved this run = the (selection-filtered) groups'
        # primaries + companions. This sizes the disk preflight (no longer the
        # whole-inbox over-count) and feeds the byte-based progress/ETA.
        move_files = [g.primary for g in groups] + [
            cpath for g in groups for cpath, _ctype in g.companions
        ]
        total_bytes = sum(p.stat().st_size for p in move_files if p.exists())
        margin_bytes = int(cfg.disk_margin_gb * (1 << 30))
        if on_plan is not None:  # report the plan totals (M groups, total bytes) once
            on_plan(len(groups), total_bytes)

        if not dry_run and not assume_yes and is_first_run(index):
            if confirm is not None and not confirm(_preview(groups, inbox, library)):
                report.confirmed = False
                return report

        # When the inbox and library share a volume, each capture moves by an atomic
        # server-side rename (no bytes over the wire) — see `_same_volume` /
        # `move_engine.rename_in_place`. A rename adds zero net bytes to the volume, so
        # the disk preflight and the mid-run free-space recheck are both skipped on this
        # path (they cost SMB round trips and can never trip — nothing fills up).
        same_volume = False
        if not dry_run:
            _sweep_stale_partials(index)  # reclaim a crashed prior run's .partial files
            _finalize_renamed_pending(index)  # reconcile rename rows orphaned mid-flip
            library.mkdir(parents=True, exist_ok=True)  # so `_same_volume` can stat it
            same_volume = _same_volume(inbox, library)
            if not same_volume:
                _disk_preflight(total_bytes, library, margin_bytes)
            report.batch_id = make_batch_id(datetime.now(), secrets.token_hex(3))

        # Pass 1 — extract every group's metadata up front (skipping groups a prior
        # run already moved), so a within-run GPS pool exists before any group is
        # geocoded or moved. Chunked across fresh ExifTool daemons with restart-on-
        # failure (a daemon crash at file 15k loses ≤1 file, not the whole pass).
        # Cancel is honoured in pass 2 (the move phase), not here: extraction is
        # read-only and the chunked daemons make it crash-resilient.
        pending = [
            (idx, group)
            for idx, group in enumerate(groups)
            if dry_run or not move_engine.is_already_moved(index, group.primary)
        ]
        extracted = _extract_pass(
            pending, extractor_factory, cfg.extract_chunk_size,
            cfg.extract_max_failures, report,
        )

        # Infer locations for no-GPS-but-timestamped captures from time-adjacent
        # GPS-bearing captures in this same batch (conservative: no neighbor in
        # window -> the file quarantines as usual, never relocated far).
        inferred_map = _infer_batch(extracted, cfg.inference_max_gap_minutes)

        # Pass 2 — geocode + group-atomic move each capture (no extractor needed).
        bytes_since_check = 0
        for idx, group, md in extracted:
            if report.aborted:
                break
            if cancel is not None and cancel():
                report.cancelled = True
                break
            # Size the group's sources BEFORE the move (they are deleted by it).
            grp_bytes = sum(
                p.stat().st_size
                for p in [group.primary, *(c for c, _t in group.companions)]
                if p.exists()
            )
            # Mid-run free-space recheck, BEFORE committing to this group and throttled
            # to ~once per GB moved (an SMB round trip per check). It requires room for
            # THIS group's bytes AND the margin, so a large clip cannot eat into the
            # configured headroom between rechecks. Checking before (not after) the move
            # means a run that just fits is never falsely aborted on its last group, and
            # an abort leaves this group + the rest in the inbox.
            if not dry_run and not same_volume and bytes_since_check >= _FREESPACE_RECHECK_BYTES:
                bytes_since_check = 0
                if shutil.disk_usage(library).free < grp_bytes + margin_bytes:
                    report.aborted = True
                    report.failures.append(
                        f"aborted mid-run: free space at {library} fell below the "
                        f"{cfg.disk_margin_gb} GB margin"
                    )
                    break
            _process_group(group, md, inferred_map.get(idx), index, geonames,
                           library, report, dry_run, progress, cfg.feature_proximity_km,
                           byte_progress, cfg.retain_hyperlapse_frames,
                           cfg.copy_retry_attempts, cfg.copy_retry_backoff_s,
                           same_volume=same_volume, inbox=inbox,
                           relocate_duplicates=cfg.relocate_duplicates)
            bytes_since_check += grp_bytes

        if not dry_run:
            _apply_catalog_ratings(
                index, report, pre.unclaimed, cfg, archive=selected_primaries is None
            )
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


def _infer_batch(extracted, max_gap_minutes: float) -> dict:
    """Within-run neighbor-GPS inference over a pass-1 ``(idx, group, md)`` list.

    Clusters on the RAW naive capture timestamp (``tz_resolver._parse_naive``) —
    the same clock for sources and targets, so same-media-type captures in a
    session compare correctly. A photo (local wall-clock) vs video (UTC)
    cross-match is skewed by the local UTC offset and will usually fall outside the
    window, so the no-GPS file simply quarantines (never a wrong location).
    Returns ``{idx: InferenceResult}`` for the no-GPS captures that found a neighbor.
    """
    items = [
        (
            idx,
            tz_resolver._parse_naive(md.capture_ts_raw) if md.capture_ts_raw else None,
            (md.lat, md.lon) if md.lat is not None and md.lon is not None else None,
        )
        for idx, _group, md in extracted
    ]
    return inference.infer_locations(items, max_gap=timedelta(minutes=max_gap_minutes))


def _apply_catalog_ratings(index, report, unclaimed, cfg, *, archive=True) -> None:
    """Read DJI MISC-catalog star ratings -> ``files.star_rating``, then archive the
    catalog DBs outside ``library_root`` (B11).

    A no-coordinate / stale / corrupt / ambiguous catalog yields no ratings and never
    raises (``misc_parser`` is fail-safe; the apply loop is plain SQL). When
    ``archive`` is true, every ``MISC/*.db`` is preserved by a crash-safe copy to
    ``<index_db_dir>/catalogs/<batch_id>/`` and its inbox source deleted, so the move
    is logged in ``moves`` (``file_id`` NULL) and ``undo`` reverses it. A partial
    import passes ``archive=False`` so the catalog stays in the inbox (ratings are
    still applied to the files filed this run) and is archived by a later full import.
    """
    dbs = [p for p in unclaimed if p.suffix.lower() == ".db"]
    if not dbs:
        return

    # Recover each organized file's lowercased dest filename. The catalog keys are the
    # original DJI basenames, which are a SUFFIX of the renamed dest stem.
    organized = [
        (fid, Path(_strip(dest)).name.lower())
        for fid, dest in index.execute(
            "SELECT id, dest_path FROM files WHERE batch_id=? AND status='organized'",
            (report.batch_id,),
        ).fetchall()
    ]
    catalogs = {p: misc_parser.read_ratings(p) for p in dbs}
    chosen = misc_parser.select_catalog(catalogs, [name for _fid, name in organized])
    if chosen is not None:
        ratings = catalogs[chosen]
        for fid, name in organized:
            # Prefer the longest matching basename so a (theoretical) shorter suffix
            # never shadows the full DJI name.
            match = max((cb for cb in ratings if name.endswith(cb)), key=len, default=None)
            if match is not None:
                index.execute(
                    "UPDATE files SET star_rating=? WHERE id=?", (ratings[match], fid)
                )
                report.ratings_applied += 1
        index.commit()

    archive_dir = Path(cfg.index_db_path).parent / "catalogs" / report.batch_id

    if not archive:
        # Partial import: PRESERVE the catalog's bytes against a card pull, but KEEP
        # the inbox copy (no destructive move, no moves row) so a later import can
        # still apply ratings to the not-yet-filed media. A verified byte-for-byte
        # copy into the same archive dir; idempotent (skip when an identical copy is
        # already there from a crash-resume), and never raises (the media is filed).
        for p in dbs:
            dest = archive_dir / p.name
            try:
                src_sha = move_engine.sha256_file(p)
                if dest.exists() and move_engine.sha256_file(dest) == src_sha:
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dest)
                if move_engine.sha256_file(dest) != src_sha:
                    dest.unlink(missing_ok=True)
                    report.failures.append(f"{p}: catalog preserve-copy verify failed")
            except OSError as exc:
                report.failures.append(f"{p}: catalog preserve-copy error: {exc}")
        return

    for p in dbs:
        dest = str(archive_dir / p.name)
        try:
            if move_engine.is_already_moved(index, p):
                continue
            out = move_engine.copy_and_verify(index, report.batch_id, p, dest)
            if out.status == "failed":
                report.failures.append(f"{p}: catalog archive failed: {out.error}")
                continue
            move_engine.commit_delete(index, p, out.source_sha256)
            # The .db is no longer stranded in the inbox: it left the unclaimed set.
            report.unclaimed -= 1
        except OSError as exc:
            # A catalog vanishing mid-run (removable media) or any IO error must NEVER
            # abort the post-move bookkeeping — the media is already filed. Record it
            # and move on; the .db simply stays where it is.
            report.failures.append(f"{p}: catalog archive error: {exc}")


def _process_group(group, md, inferred, index, geonames, library, report, dry_run,
                   progress, feature_proximity_km=5.0, byte_progress=None,
                   retain_hyperlapse_frames=True, copy_retry_attempts=1,
                   copy_retry_backoff_s=0.0, same_volume=False, inbox=None,
                   relocate_duplicates=False) -> None:
    primary = group.primary

    # Effective companion set: a hyperlapse group with retention off files the render
    # alone (its frames stay in the inbox), so they leave the move/persist set here.
    companions = group.companions
    if group.capture_kind == "hyperlapse" and not retain_hyperlapse_frames:
        companions = [(p, t) for p, t in companions if t != "hyperlapse_frame"]
    frame_count = (
        sum(1 for _, t in companions if t in ("hyperlapse_frame", "panorama_frame"))
        if group.capture_kind in ("hyperlapse", "panorama")
        else None
    )
    # Source sizes for the retained-frame report. Guarded with ``exists()`` because a
    # crash-resume can re-enter here with some frame sources already deleted (the
    # primary's source_deleted sentinel is what gates a full skip, not per-frame).
    frame_bytes = sum(
        p.stat().st_size
        for p, t in companions
        if t == "hyperlapse_frame" and p.exists()
    )

    if md.media_type == "video":  # codec stats are a video-only tally (HEVC decision)
        report.codec[md.codec if md.codec in ("h264", "h265") else "unknown"] += 1
    if progress is not None:
        progress(f"  {primary.name}")

    # Borrow a time-adjacent neighbor's GPS when this capture has none (B8). A
    # hyperlapse render already borrowed its frame GPS in pass 1, so this only
    # applies to the rare frame-less / GPS-less case. The borrowed coordinate then
    # flows through tz/geocode/path exactly like real GPS.
    if md.lat is None and md.lon is None and inferred is not None:
        md = replace(md, lat=inferred.lat, lon=inferred.lon, gps_source="inferred")
    was_inferred = md.gps_source == "inferred"

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
        _tally(report, companions, geo, quarantine, local, was_inferred, frame_bytes)
        return

    # Dedup hash for the primary. The primary is moved LAST on the rename path, so it
    # is normally present here; it is absent only on crash recovery (already renamed but
    # the moves row never reached source_deleted), where it is already filed — skip the
    # dedup check (rename_in_place recovers its hash from the pending row).
    if primary.exists():
        src_sha = move_engine.sha256_file(primary)
        if _is_duplicate(index, primary, src_sha):
            report.duplicates_skipped += 1
            if relocate_duplicates and inbox is not None:
                _relocate_duplicate(index, group, companions, inbox, src_sha, report)
            return
    else:
        src_sha = None
    primary_dest = _resolve_collision(index, primary, primary_dest)

    files_to_move = [(primary, primary_dest)]
    for cpath, ctype in companions:
        cdest = (
            _frame_dest(primary_dest, primary, cpath)
            if ctype in ("hyperlapse_frame", "panorama_frame")
            else _companion_dest(primary_dest, primary, cpath)
        )
        files_to_move.append((cpath, cdest))

    # SAFETY (recycled-filename collision): a capture group must never resolve two of
    # its own files to one destination. Per-directory grouping makes this impossible,
    # but guard it so any future regression skips the group loudly instead of letting
    # the move engine overwrite one file with another.
    dests = [dp for _sp, dp in files_to_move]
    if len(set(dests)) != len(dests):
        dupes = sorted({d for d in dests if dests.count(d) > 1})
        report.failures.append(
            f"{primary.name}: capture group resolved to duplicate destination(s) "
            f"{dupes}; skipped to avoid overwrite"
        )
        return

    if same_volume:
        # Same-volume fast path: atomic server-side rename (no copy, no verify read-back,
        # no separate delete — os.replace moves zero bytes and removes the source
        # atomically). It stays GROUP-ATOMIC the same way the copy path is: the whole
        # group is recorded in the move log and its files/file_companions rows are
        # persisted while every source is STILL in the inbox (steps 1-2), and only THEN
        # is each file renamed (step 3, companions first, primary LAST). So the primary's
        # source_deleted (the group-done sentinel Pass 1's `is_already_moved` skip relies
        # on) can never exist without a durable files row — a crash never orphans an
        # unindexed file in the library; it leaves a recoverable partial group.

        # 1. Record a pending moves row for every file whose source is still present
        #    (so we always hash the CURRENT bytes — never trust a stale prior-run row's
        #    hash for a file the user may have swapped). A source that is already gone was
        #    renamed by a prior crashed run; it is left for step 3's rename_in_place to
        #    finalize from its existing row. record_pending keeps every present source in
        #    the inbox at this point.
        shas: dict[Path, str] = {}
        for sp, dp in files_to_move:
            if not sp.exists():
                continue
            sha = src_sha if (sp == primary and src_sha is not None) else move_engine.sha256_file(sp)
            move_engine.record_pending(index, report.batch_id, sp, dp, sha)
            shas[sp] = sha
        # 2. Persist files/file_companions/moves-link BEFORE any source leaves the inbox.
        primary_sha = shas.get(primary) or _row_sha(index, primary)
        _persist(index, report, md, geo, local, quarantine, primary, primary_dest,
                 companions, files_to_move, primary_sha, group.capture_kind, frame_count)
        # 3. Finalize each move with an atomic rename — companions first, primary LAST.
        #    rename_in_place handles a source already gone (resume) via its recovery path.
        for sp, dp in reversed(files_to_move):
            if move_engine.is_already_moved(index, sp):
                continue
            outcome = move_engine.rename_in_place(
                index, report.batch_id, sp, dp, source_sha256=shas.get(sp),
            )
            if outcome.status == "failed":
                report.aborted = True
                report.failures.append(f"{sp}: {outcome.error}")
                return  # files row already durable; re-run completes the renames
        _tally(report, companions, geo, quarantine, local, was_inferred, frame_bytes)
        return

    # Phase A: copy + verify every file in the group (no deletes yet). Record each
    # file's verified source hash so Phase B's delete keys on the exact moves row.
    shas: dict[Path, str] = {}
    for sp, dp in files_to_move:
        if move_engine.is_already_moved(index, sp):
            continue
        bp = (
            (lambda phase, done, total, _name=sp.name: byte_progress(_name, phase, done, total))
            if byte_progress is not None
            else None
        )
        # Reuse the primary's dedup hash (computed above) so copy_and_verify does
        # not re-read the primary just to re-hash it (companions have no pre-hash).
        # Per-file retry+backoff rides a transient SMB blip without aborting the batch.
        outcome = move_engine.copy_and_verify(
            index, report.batch_id, sp, dp,
            source_sha256=src_sha if sp == primary else None,
            progress=bp,
            retry_attempts=copy_retry_attempts,
            retry_backoff_s=copy_retry_backoff_s,
        )
        if outcome.status == "failed":
            report.aborted = True
            report.failures.append(f"{sp}: {outcome.error}")
            return  # group-atomic: delete NO source in this group, halt the batch
        shas[sp] = outcome.source_sha256

    # Phase B: persist the index rows, then delete sources — companions first, the
    # primary last, so the primary's source_deleted row is a reliable group-done
    # sentinel. Delete keys on the stored hash, never a re-hash of the live file.
    primary_sha = shas.get(primary) or _stored_sha(index, primary)
    _persist(index, report, md, geo, local, quarantine, primary, primary_dest,
             companions, files_to_move, primary_sha, group.capture_kind, frame_count)
    for sp, _dp in reversed(files_to_move):
        sha = shas.get(sp)
        if sha is not None:  # already-deleted files (skipped in Phase A) need nothing
            move_engine.commit_delete(index, sp, sha)

    _tally(report, companions, geo, quarantine, local, was_inferred, frame_bytes)


def _persist(index, report, md, geo, local, quarantine, primary, primary_dest,
             companions, files_to_move, primary_sha, capture_kind, frame_count) -> None:
    """Insert/refresh the ``files`` row and its ``file_companions`` (idempotent)."""
    row = index.execute("SELECT id FROM files WHERE dest_path=?", (primary_dest,)).fetchone()
    if row is not None:
        file_id = row[0]
    else:
        cur = index.execute(
            "INSERT INTO files(geonameid, place_string, dest_path, filename, media_type, "
            "capture_ts_utc, capture_ts_local, local_date, lat, lon, gps_source, "
            "geocode_confidence, tz_ambiguous, codec, width, height, duration_s, sha256, "
            "status, batch_id, capture_kind, frame_count) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                capture_kind,
                frame_count,
            ),
        )
        file_id = cur.lastrowid

    # Link the primary's moves row and (re)write companion rows idempotently.
    index.execute(
        "UPDATE moves SET file_id=? WHERE source_path=? AND file_id IS NULL",
        (file_id, str(primary)),
    )
    index.execute("DELETE FROM file_companions WHERE primary_file_id=?", (file_id,))
    for (cpath, ctype), (_sp, cdest) in zip(companions, files_to_move[1:]):
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


def _row_sha(index, source: Path) -> str:
    """Source hash from this source's most recent ``moves`` row (any status).

    Used on the same-volume path where a file may already be recorded (a prior crashed
    run's ``pending`` row) or even already renamed away — so its hash must come from the
    row, not from re-reading a possibly-absent source. Falls back to hashing the live
    file only when no row exists (which implies the source is still present).
    """
    row = index.execute(
        "SELECT source_sha256 FROM moves WHERE source_path=? ORDER BY id DESC LIMIT 1",
        (str(source),),
    ).fetchone()
    return row[0] if row else move_engine.sha256_file(source)


def _tally(report, companions, geo, quarantine, local, was_inferred=False,
           frame_bytes=0) -> None:
    if quarantine:
        report.quarantined += 1
    else:
        report.organized += 1
        if was_inferred:
            report.inferred += 1
        place = (geo.place_string if geo and geo.place_string else "_unknown")
        report.per_place[place] = report.per_place.get(place, 0) + 1
    report.companions += len(companions)
    report.retained_frame_bytes += frame_bytes
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
