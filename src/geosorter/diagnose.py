"""Read-only inbox diagnostic — explain why each inbox file is (not) organized.

``organize`` removes a source from the inbox ONLY when it copies+verifies it into
the library or quarantine. A file that PERSISTS across runs is being skipped for a
reason that today leaves no per-file trace — chiefly a SILENT duplicate-hash skip
(:func:`geosorter.organize._is_duplicate` bumps a bare ``duplicates_skipped``
counter and returns, logging no path), but also non-DJI clutter, orphaned sidecars,
and unlinked hyperlapse/panorama frame directories that the pre-scan drops or only
notes as a ``warnings`` string.

:func:`diagnose_inbox` accounts for EVERY inbox file with a disposition + reason,
reusing the real grouping / extraction / dedup logic so the verdict matches what
``organize`` actually does — unlike ``organize --dry-run``, which skips the duplicate
check entirely (the dry-run path returns in ``_tally`` before the hash compare).

Strictly read-only: it performs no moves and writes no data rows. The index DB is
opened only for SELECT lookups; the idempotent :func:`geosorter.db.init_index_schema`
schema-ensure (so the ``files``/``moves`` tables exist on a never-organized library)
is the same benign idiom used by ``verify-library``/``undo``/``rescan``. The geonames
DB is never opened — the quarantine verdict needs only GPS presence + a resolvable
local date (``timezonefinder``-backed), not reverse geocoding.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import db, grouping, move_engine, organize, tz_resolver
from .metadata import MetadataExtractor

# Disposition vocabulary (the ``FileDiagnosis.disposition`` values).
WOULD_ORGANIZE = "would-organize"
WOULD_QUARANTINE = "would-quarantine"
DUPLICATE = "duplicate"
ALREADY_MOVED = "already-moved"
NON_DJI_CLUTTER = "non-dji-clutter"
ORPHANED_SIDECAR = "orphaned-sidecar"
UNLINKED_FRAME_DIR = "unlinked-frame-dir"
MISC_CATALOG = "misc-catalog"


@dataclass(frozen=True)
class FileDiagnosis:
    """One inbox file's verdict.

    ``disposition`` is one of the module constants; ``reason`` is a short
    human-readable explanation; ``detail`` carries an extra path when relevant (the
    existing library ``dest_path`` for a ``duplicate``).
    """

    path: Path
    disposition: str
    reason: str
    detail: str | None = None


@dataclass(frozen=True)
class InboxDiagnosis:
    """Outcome of :func:`diagnose_inbox` — one entry per inbox file plus a tally."""

    files: list[FileDiagnosis]
    counts: dict[str, int]  # disposition -> file count


class _Sink:
    """Minimal stand-in for :class:`organize.BatchReport`.

    :func:`organize._extract_one` only ever touches ``.failures`` (it appends one
    entry when a file is unreadable after every retry); reusing it keeps the
    extraction / daemon-restart behaviour identical to ``organize`` without dragging
    in the whole ``BatchReport``.
    """

    def __init__(self) -> None:
        self.failures: list[str] = []


def diagnose_inbox(
    cfg,
    *,
    hash_check: bool = True,
    progress=None,
    extractor_factory=MetadataExtractor,
) -> InboxDiagnosis:
    """Classify every file under ``cfg.inbox_path`` by why it will / won't organize.

    Mirrors :func:`geosorter.inbox.count_inbox`'s ``rglob`` + ``prescan_inbox`` scan,
    then for each capture group computes the same verdict ``organize`` would: an
    already-moved group (``move_engine.is_already_moved``), an unreadable file, a
    no-GPS / no-date quarantine, a content ``duplicate`` (the silent
    ``organize._is_duplicate`` skip), or ``would-organize``. Files outside any group
    are explained as MISC catalogs, unlinked frame-dir leftovers, orphaned sidecars,
    or non-DJI clutter, so the report is exhaustive over the inbox.

    ``hash_check=False`` skips the per-primary SHA-256 read (the expensive bit over
    SMB), giving a fast structural triage; duplicates are then not detected and such
    captures report their structural verdict (usually ``would-organize``). ``progress``
    is a one-arg per-capture callback (mirrors ``run_rescan``). ``extractor_factory``
    is injectable for tests. Returns ``InboxDiagnosis([], {})`` when the inbox is unset
    or absent.

    Two deliberate simplifications vs ``organize`` (neither affects the *stuck-file*
    diagnosis — the point of this tool):

    * The two-pass neighbor-GPS inference (a no-GPS-but-timestamped capture borrowing
      a time-adjacent sibling's location, ``cfg.inference_max_gap_minutes``) is NOT
      modeled, so such a capture reports ``would-quarantine`` (``no-gps``) here even
      though ``organize`` may file it with ``gps_source='inferred'``. Both
      ``would-quarantine`` and ``would-organize`` LEAVE the inbox, so this never
      mislabels a *persistent* file — only the four buckets ``duplicate`` /
      ``orphaned-sidecar`` / ``non-dji-clutter`` / ``unlinked-frame-dir`` actually stay.
    * ``already-moved`` and ``duplicate`` are two faces of "already in the library":
      the former is THIS source path already filed (an idempotent re-drop at its
      original path), the latter is identical content re-imported under a *different*
      path. Both correctly predict "won't move."
    """
    if cfg.inbox_path is None:
        return InboxDiagnosis(files=[], counts={})
    inbox = Path(cfg.inbox_path)
    if not inbox.is_dir():
        return InboxDiagnosis(files=[], counts={})

    paths = [p for p in sorted(inbox.rglob("*")) if p.is_file()]
    pre = grouping.prescan_inbox(paths, inbox_root=inbox)

    index = db.connect(cfg.index_db_path, integrity_check=False)
    db.init_index_schema(index)  # tolerate a never-organized library (no files table yet)
    results: list[FileDiagnosis] = []
    try:
        claimed: set[Path] = set()
        extractor = None  # started lazily so a clutter-only inbox never spawns ExifTool
        sink = _Sink()
        try:
            for group in pre.groups:
                primary = group.primary
                claimed.add(primary)
                for cpath, _ctype in group.companions:
                    claimed.add(cpath)
                if progress is not None:
                    progress(primary.name)

                if move_engine.is_already_moved(index, primary):
                    disp, reason, detail = (
                        ALREADY_MOVED,
                        "already filed by a prior run (source_deleted)",
                        None,
                    )
                else:
                    if extractor is None:
                        extractor = extractor_factory()
                        extractor.__enter__()
                    before = len(sink.failures)
                    md, extractor = organize._extract_one(
                        group, extractor, extractor_factory, cfg.extract_max_failures, sink
                    )
                    if len(sink.failures) > before:
                        disp, reason, detail = WOULD_QUARANTINE, "unreadable", None
                    else:
                        disp, reason, detail = _verdict(md, primary, index, hash_check)

                results.append(FileDiagnosis(primary, disp, reason, detail))
                for cpath, _ctype in group.companions:
                    results.append(
                        FileDiagnosis(cpath, disp, f"companion of {primary.name}", detail)
                    )
        finally:
            if extractor is not None:
                organize._close_extractor(extractor)

        unclaimed = set(pre.unclaimed)
        for p in pre.unclaimed:
            results.append(_unclaimed_diag(p, inbox))

        for p in paths:
            if p in claimed or p in unclaimed:
                continue
            results.append(_leftover_diag(p, inbox))
    finally:
        index.close()

    results.sort(key=lambda d: str(d.path))
    counts: dict[str, int] = {}
    for d in results:
        counts[d.disposition] = counts.get(d.disposition, 0) + 1
    return InboxDiagnosis(files=results, counts=counts)


def _verdict(md, primary: Path, index, hash_check: bool):
    """The duplicate / quarantine / would-organize verdict for a readable capture.

    Ordering MIRRORS :func:`organize._process_group`: the duplicate hash check
    preempts the quarantine/organize decision. ``organize`` reaches its dedup skip
    AFTER computing ``quarantine`` but returns early on a duplicate REGARDLESS of
    quarantine status (organize.py — ``if _is_duplicate(...): duplicates_skipped +=
    1; return``, which runs for both the no-GPS and the geocoded branch). So a no-GPS
    / no-date capture whose content is already in the library is the SILENT
    ``duplicate`` skip it actually is — not a ``would-quarantine`` (the misdiagnosis
    this tool exists to avoid).
    """
    if hash_check and primary.exists():
        src_sha = move_engine.sha256_file(primary)
        if organize._is_duplicate(index, primary, src_sha):
            row = index.execute(
                "SELECT dest_path FROM files WHERE sha256=? LIMIT 1", (src_sha,)
            ).fetchone()
            dest = organize._strip(row[0]) if row else None
            return DUPLICATE, "identical content already in the library", dest
    if md.lat is None or md.lon is None:
        return WOULD_QUARANTINE, "no-gps", None
    local = tz_resolver.resolve_local_time(
        md.lat, md.lon, md.capture_ts_raw, md.capture_ts_source_tag
    )
    if local.local_date is None:
        return WOULD_QUARANTINE, "no-date", None
    return WOULD_ORGANIZE, "gps + date present", None


def _unclaimed_diag(p: Path, inbox: Path) -> FileDiagnosis:
    """Classify a ``prescan_inbox.unclaimed`` path (MISC catalogs / malformed layouts)."""
    try:
        kind, _counter = grouping._classify(p.relative_to(inbox).parts)
    except ValueError:
        kind = "flat"
    if kind == "misc" or p.suffix.lower() == ".db":
        return FileDiagnosis(
            p, MISC_CATALOG, "DJI MISC catalog (archived by organize, not stuck)", None
        )
    return FileDiagnosis(
        p, UNLINKED_FRAME_DIR, "malformed HYPERLAPSE/PANORAMA frame layout", None
    )


def _leftover_diag(p: Path, inbox: Path) -> FileDiagnosis:
    """Classify an inbox file that ``prescan_inbox`` neither grouped nor unclaimed.

    These are: a hyperlapse/panorama frame whose directory failed to link to a render
    (left in place with only a ``warnings`` note), an orphaned DJI sidecar (no primary
    in its directory, or one outside the sidecar mtime window), or non-DJI clutter.
    """
    try:
        kind, _counter = grouping._classify(p.relative_to(inbox).parts)
    except ValueError:
        return FileDiagnosis(p, NON_DJI_CLUTTER, "not a DJI capture filename", None)
    if kind in ("hyperlapse", "panorama"):
        return FileDiagnosis(
            p, UNLINKED_FRAME_DIR, "frame not linked to a render — left in the inbox", None
        )
    if grouping._parse(p) is not None and p.suffix.lower() in grouping._COMPANION_EXT:
        return FileDiagnosis(
            p,
            ORPHANED_SIDECAR,
            "DJI sidecar not attached to a primary "
            "(none in its directory, or outside the sidecar mtime window)",
            None,
        )
    return FileDiagnosis(p, NON_DJI_CLUTTER, "not a DJI capture filename", None)
