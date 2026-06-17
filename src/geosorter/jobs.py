"""Background ``organize`` job manager for the HTTP API (B6).

The map viewer triggers ``organize`` over HTTP, but a full library scan is far too
long for a request/response cycle. This manager runs each scan on a dedicated
single-worker thread pool (``max_workers=1`` — only one destructive pass at a
time), tracks per-job state by UUID for status polling, and exposes a cooperative
cancel flag wired into :func:`geosorter.organize.run_organize`'s ``cancel`` hook.

Deliberately **not** FastAPI ``BackgroundTasks``: those are tied to a single
request's lifecycle and offer no id, status, or cancellation.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace

from . import config, db, pathing
from .derived import (
    HuginNotFound,
    StitchFailed,
    classify_stitched_projection,
    panorama_stitch,
    stitch_cache_path,
)
from .organize import run_organize
from .rescan import run_rescan
from .retag import retag_file
from .setloc import assign_locations
from .undo import run_undo
from .warm import warm_library

logger = logging.getLogger("geosorter.jobs")


def _strip(dest_path: str) -> str:
    """Drop the Windows ``\\\\?\\`` long-path prefix if present."""
    return dest_path[4:] if dest_path.startswith("\\\\?\\") else dest_path


def _compute_eta(total_bytes: int, done_bytes: int, elapsed_s: float) -> float | None:
    """Seconds remaining at the observed byte rate, or ``None`` when not estimable.

    Returns ``None`` until there is something to do AND some progress AND elapsed time
    to derive a rate from (so a just-started job reports no ETA rather than a wild
    guess). Bytes-based, since file sizes vary by orders of magnitude (a 72 MP JPEG
    vs a multi-GB HEVC clip), so a per-file count would be a poor predictor.
    """
    if total_bytes <= 0 or done_bytes <= 0 or elapsed_s <= 0:
        return None
    rate = done_bytes / elapsed_s
    return max(0.0, total_bytes - done_bytes) / rate


class WorkerBusy(Exception):
    """A destructive job was submitted while another already holds the single worker.

    Raised by ``submit_undo``/``submit_retag``/``submit_rescan`` so the HTTP layer can
    answer ``409`` with the blocking job id instead of silently queueing the request
    behind a multi-hour ``organize`` run. ``blocking_job_id`` is that in-flight job.
    """

    def __init__(self, blocking_job_id: str):
        super().__init__(f"a destructive job ({blocking_job_id}) is already running")
        self.blocking_job_id = blocking_job_id


@dataclass
class JobState:
    """Serializable snapshot of one organize job's progress."""

    job_id: str
    state: str = "pending"  # pending | running | done | error | cancelled
    organized: int = 0
    quarantined: int = 0
    duplicates_skipped: int = 0
    companions: int = 0
    processed: int = 0  # files seen so far (progress callback ticks) == captures done
    current: str | None = None  # most recent file being processed
    current_phase: str | None = None  # 'hashing' | 'copying' | 'verifying' (byte progress)
    bytes_done: int = 0  # bytes processed in the current file's current phase
    bytes_total: int = 0  # current file's size (0 until a byte-progress tick arrives)
    total_groups: int = 0  # M: capture groups this run will move (from on_plan)
    total_bytes: int = 0  # total source bytes this run will move (from on_plan)
    bytes_done_total: int = 0  # bytes of fully-completed files so far (for the ETA)
    eta_seconds: float | None = None  # bytes-based estimate, recomputed per status()
    error: str | None = None
    failures: list[str] = field(default_factory=list)


@dataclass
class UndoJobState:
    """Serializable snapshot of one undo job's progress."""

    job_id: str
    state: str = "pending"  # pending | running | done | error | cancelled
    batch_id: str | None = None
    restored: int = 0
    missing: int = 0
    processed: int = 0  # files seen so far (progress callback ticks)
    current: str | None = None  # most recent file being reversed
    nothing_to_undo: bool = False
    error: str | None = None
    conflicts: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


@dataclass
class RetagJobState:
    """Serializable snapshot of one manual re-tag job's progress."""

    job_id: str
    state: str = "pending"  # pending | running | done | error
    status: str = ""  # the RetagReport status: 'retagged' | 'not_found' | 'failed'
    moved: int = 0
    place_string: str | None = None
    processed: int = 0  # files seen so far (progress callback ticks)
    current: str | None = None  # most recent file being relocated
    error: str | None = None


@dataclass
class AssignJobState:
    """Serializable snapshot of one bulk assign-location job's progress.

    Promotes one or more no-GPS (quarantined) captures to organized by assigning a
    user-picked coordinate (see :mod:`geosorter.setloc`). Shares the destructive
    worker with organize/undo/retag/rescan; no cancel (an atomic bulk re-file).
    """

    job_id: str
    state: str = "pending"  # pending | running | done | error
    assigned: int = 0       # captures promoted quarantine -> organized
    skipped: int = 0        # unknown / already-organized ids
    place_string: str | None = None
    total: int = 0          # selected captures (set at submit) — the progress denominator
    processed: int = 0      # files seen so far (progress callback ticks)
    current: str | None = None  # most recent file being relocated
    error: str | None = None
    failures: list[str] = field(default_factory=list)


@dataclass
class RescanJobState:
    """Serializable snapshot of one rescan job's progress."""

    job_id: str
    state: str = "pending"  # pending | running | done | error
    checked: int = 0
    pruned: int = 0
    kept: int = 0
    processed: int = 0  # files seen so far (progress callback ticks)
    current: str | None = None  # most recent file checked
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    orphaned: list[str] = field(default_factory=list)


@dataclass
class WarmJobState:
    """Serializable snapshot of one post-organize warm-pass job (m-derived-at-scale)."""

    job_id: str
    state: str = "pending"  # pending | running | done | error
    batch_id: str | None = None
    warmed: int = 0  # thumbs/posters generated (or already fresh) for the batch
    proxies_warmed: int = 0  # HEVC→H.264 proxies generated or already cached (m-implement-proxy-prewarm-cap)
    deleted: int = 0  # local-tier cache files evicted at the end
    skipped: int = 0  # eviction skips (open file on Windows)
    processed: int = 0  # files seen so far (progress callback ticks)
    current: str | None = None  # most recent file warmed
    error: str | None = None


@dataclass
class StitchJobState:
    """Serializable snapshot of one panorama-stitch job's progress (B13)."""

    job_id: str
    state: str = "pending"  # pending | running | done | error
    file_id: int | None = None
    # '' (in progress) | 'ok' | 'failed' (degenerate/pipeline error) | 'unavailable'
    # (Hugin not installed — the row keeps NULL and the UI keeps the tile gallery)
    status: str = ""
    # Live Hugin pipeline progress: which of the six steps is currently running, so
    # the map UI can show "step 3/6: cpclean" during the multi-minute stitch.
    step: int = 0
    step_total: int = 6
    step_name: str = ""
    # The detected projection of a successful hero (m-fix-panorama-projection-autodetect):
    # 'equirectangular' (PanoSphere 360 viewer) | 'flat' (flat zoomable image) | '' while
    # in progress / on a cache hit. Lets the lightbox pick its viewer before the library
    # reload brings files.stitch_projection.
    projection: str = ""
    error: str | None = None


class JobManager:
    """Owns the worker pool and the live job table.

    ``organize_fn``/``undo_fn``/``retag_fn`` are injectable for tests; they default
    to the real :func:`geosorter.organize.run_organize` /
    :func:`geosorter.undo.run_undo` / :func:`geosorter.retag.retag_file`. The single
    ``max_workers=1`` executor is shared by all three job kinds, so organize, undo,
    and re-tag can never run concurrently against the same library/index.
    """

    def __init__(self, cfg, *, organize_fn=run_organize, undo_fn=run_undo,
                 retag_fn=retag_file, rescan_fn=run_rescan, stitch_fn=panorama_stitch,
                 warm_fn=warm_library, assign_fn=assign_locations):
        self._cfg = cfg
        self._organize_fn = organize_fn
        self._undo_fn = undo_fn
        self._retag_fn = retag_fn
        self._rescan_fn = rescan_fn
        self._stitch_fn = stitch_fn
        self._warm_fn = warm_fn
        self._assign_fn = assign_fn
        self._executor = ThreadPoolExecutor(max_workers=1)
        # A panorama stitch is read-only (~7 min) and strictly off the crash-safe
        # move path, so it gets its OWN single worker: stitches serialize among
        # themselves but never block (or wait behind) organize/undo/retag.
        self._stitch_pool = ThreadPoolExecutor(max_workers=1)
        # The post-organize warm pass is likewise read-only (regenerates cache only)
        # and gets its OWN single worker — it runs ALONGSIDE the destructive pool
        # (auto-triggered right as an organize finishes) without blocking it.
        self._warm_pool = ThreadPoolExecutor(max_workers=1)
        self._jobs: dict[str, JobState] = {}
        self._undo_jobs: dict[str, UndoJobState] = {}
        self._retag_jobs: dict[str, RetagJobState] = {}
        self._assign_jobs: dict[str, AssignJobState] = {}
        self._rescan_jobs: dict[str, RescanJobState] = {}
        self._stitch_jobs: dict[str, StitchJobState] = {}
        self._warm_jobs: dict[str, WarmJobState] = {}
        self._cancels: dict[str, threading.Event] = {}
        # Monotonic start time per organize job, for the bytes-based ETA. Kept off
        # JobState so it never leaks into the serialized status response.
        self._job_started: dict[str, float] = {}
        self._lock = threading.Lock()

    def submit(self, selected_primaries: set[str] | None = None) -> str:
        """Queue a new organize job and return its UUID job id.

        ``selected_primaries`` (None = import everything) is forwarded to
        :func:`run_organize` so the map UI can import a chosen subset of the inbox.
        """
        job_id = uuid.uuid4().hex
        with self._lock:
            self._jobs[job_id] = JobState(job_id=job_id)
            self._cancels[job_id] = threading.Event()
        self._executor.submit(self._run, job_id, selected_primaries)
        return job_id

    def status(self, job_id: str) -> JobState | None:
        """Point-in-time snapshot of a job's state, or ``None`` if id is unknown.

        Returns a copy taken under the lock so a caller (e.g. an HTTP request
        thread serializing the state) sees a consistent snapshot rather than a
        live object the worker thread is concurrently mutating. Progress fields
        are eventually-consistent: a later call reflects newer progress.
        """
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None:
                return None
            snap = replace(state)
            started = self._job_started.get(job_id)
        # ETA is only meaningful while running. A finished job has zero remaining; a
        # pending/error/cancelled job has no honest estimate. (Mid-run the estimate is
        # a lower bound: dedup-skipped sources are counted in total_bytes but never
        # flow through byte_progress, so done lags total — acceptable for a hint.)
        if snap.state == "done":
            snap.eta_seconds = 0.0
        elif snap.state == "running" and started is not None:
            snap.eta_seconds = _compute_eta(
                snap.total_bytes,
                snap.bytes_done_total + snap.bytes_done,
                time.monotonic() - started,
            )
        else:
            snap.eta_seconds = None
        return snap

    def cancel(self, job_id: str) -> bool:
        """Request cancellation; returns ``False`` for an unknown job id."""
        event = self._cancels.get(job_id)
        if event is None:
            return False
        event.set()
        return True

    def _active_destructive_job(self) -> str | None:
        """Id of an in-flight (pending/running) organize/undo/retag/rescan job, else None.

        The four kinds share the single destructive worker, so at most one runs at a
        time and the rest would queue. Caller must hold ``self._lock``. The stitch pool
        is independent and deliberately excluded. ``_jobs`` (organize) is scanned first
        so a long bulk import is reported as the blocker.
        """
        for table in (self._jobs, self._undo_jobs, self._retag_jobs,
                      self._assign_jobs, self._rescan_jobs):
            for jid, st in table.items():
                if st.state in ("pending", "running"):
                    return jid
        return None

    def _run(self, job_id: str, selected_primaries: set[str] | None = None) -> None:
        state = self._jobs[job_id]
        event = self._cancels[job_id]
        state.state = "running"
        with self._lock:
            self._job_started[job_id] = time.monotonic()
        seen = {"file": None, "total": 0}  # tracks the in-flight file for byte totals

        def progress(msg: str) -> None:
            state.processed += 1
            state.current = msg.strip()

        def on_plan(total_groups: int, total_bytes: int) -> None:
            state.total_groups = total_groups
            state.total_bytes = total_bytes

        def byte_progress(name: str, phase: str, done: int, total: int) -> None:
            # When a new file starts, the previous file's bytes are now complete —
            # fold them into the cumulative total that drives the ETA.
            if name != seen["file"]:
                if seen["file"] is not None:
                    state.bytes_done_total += seen["total"]
                seen["file"] = name
            seen["total"] = total
            state.current = name
            state.current_phase = phase
            state.bytes_done = done
            state.bytes_total = total

        try:
            report = self._organize_fn(
                self._cfg, assume_yes=True, cancel=event.is_set,
                progress=progress, byte_progress=byte_progress,
                selected_primaries=selected_primaries, on_plan=on_plan,
            )
        except Exception as exc:  # surface any pipeline failure as a job error
            state.state = "error"
            state.error = str(exc)
            return

        state.organized = report.organized
        state.quarantined = report.quarantined
        state.duplicates_skipped = report.duplicates_skipped
        state.companions = report.companions
        state.failures = list(report.failures)
        if report.cancelled:
            state.state = "cancelled"
        elif report.aborted:
            state.state = "error"
            state.error = "; ".join(report.failures) or "aborted"
        else:
            state.state = "done"

        # Warm the just-filed batch's thumbnails/posters on the dedicated warm pool
        # (off the destructive worker), so the first browse is hot. Best-effort: a
        # warm-enqueue failure never affects the organize result. Any captures that
        # landed (even on a cancelled/aborted partial run) are worth warming.
        if report.batch_id and report.organized > 0:
            try:
                self.submit_warm(report.batch_id)
            except Exception:  # pragma: no cover - defensive; enqueue is local + cheap
                logger.warning(
                    "failed to enqueue warm pass for batch %s", report.batch_id,
                    exc_info=True,
                )

    # ----- undo jobs (share the executor + cancel table with organize) ------- #

    def submit_undo(self, batch_id: str | None = None) -> str:
        """Queue a new undo job (most recent batch by default) and return its id.

        Raises :class:`WorkerBusy` if another destructive job already holds the worker.
        """
        job_id = uuid.uuid4().hex
        with self._lock:
            busy = self._active_destructive_job()
            if busy is not None:
                raise WorkerBusy(busy)
            self._undo_jobs[job_id] = UndoJobState(job_id=job_id)
            self._cancels[job_id] = threading.Event()
        self._executor.submit(self._run_undo, job_id, batch_id)
        return job_id

    def undo_status(self, job_id: str) -> UndoJobState | None:
        """Consistent point-in-time snapshot of an undo job, or ``None`` if unknown."""
        with self._lock:
            state = self._undo_jobs.get(job_id)
            return replace(state) if state is not None else None

    def _run_undo(self, job_id: str, batch_id: str | None) -> None:
        state = self._undo_jobs[job_id]
        event = self._cancels[job_id]
        state.state = "running"

        def progress(msg: str) -> None:
            state.processed += 1
            state.current = msg.strip()

        try:
            report = self._undo_fn(
                self._cfg, batch_id=batch_id, cancel=event.is_set, progress=progress
            )
        except Exception as exc:  # surface any pipeline failure as a job error
            state.state = "error"
            state.error = str(exc)
            return

        state.batch_id = report.batch_id
        state.restored = report.restored
        state.missing = report.missing
        state.nothing_to_undo = report.nothing_to_undo
        state.conflicts = list(report.conflicts)
        state.failures = list(report.failures)
        state.state = "cancelled" if report.cancelled else "done"

    # ----- re-tag jobs (share the executor with organize/undo) --------------- #

    def submit_retag(self, file_id: int, lat: float, lon: float) -> str:
        """Queue a manual re-tag of ``file_id`` to ``(lat, lon)`` and return its id.

        Raises :class:`WorkerBusy` if another destructive job already holds the worker.
        """
        job_id = uuid.uuid4().hex
        with self._lock:
            busy = self._active_destructive_job()
            if busy is not None:
                raise WorkerBusy(busy)
            self._retag_jobs[job_id] = RetagJobState(job_id=job_id)
        self._executor.submit(self._run_retag, job_id, file_id, lat, lon)
        return job_id

    def retag_status(self, job_id: str) -> RetagJobState | None:
        """Consistent point-in-time snapshot of a re-tag job, or ``None`` if unknown."""
        with self._lock:
            state = self._retag_jobs.get(job_id)
            return replace(state) if state is not None else None

    def _run_retag(self, job_id: str, file_id: int, lat: float, lon: float) -> None:
        state = self._retag_jobs[job_id]
        state.state = "running"

        def progress(msg: str) -> None:
            state.processed += 1
            state.current = msg.strip()

        try:
            report = self._retag_fn(self._cfg, file_id, lat, lon, progress=progress)
        except Exception as exc:  # surface any failure as a job error
            state.state = "error"
            state.error = str(exc)
            return

        state.status = report.status
        state.moved = report.moved
        state.place_string = report.place_string
        if report.status == "failed":
            state.state = "error"
            state.error = report.error
        else:
            state.state = "done"

    # ----- assign-location jobs (share the executor with organize/undo/retag) - #

    def submit_assign(self, file_ids, lat: float, lon: float) -> str:
        """Queue a bulk assign of ``file_ids`` to ``(lat, lon)`` and return its id.

        Raises :class:`WorkerBusy` if another destructive job already holds the worker.
        """
        job_id = uuid.uuid4().hex
        ids = list(file_ids)
        with self._lock:
            busy = self._active_destructive_job()
            if busy is not None:
                raise WorkerBusy(busy)
            self._assign_jobs[job_id] = AssignJobState(job_id=job_id, total=len(ids))
        self._executor.submit(self._run_assign, job_id, ids, lat, lon)
        return job_id

    def assign_status(self, job_id: str) -> AssignJobState | None:
        """Consistent point-in-time snapshot of an assign job, or ``None`` if unknown."""
        with self._lock:
            state = self._assign_jobs.get(job_id)
            return replace(state) if state is not None else None

    def _run_assign(self, job_id: str, file_ids, lat: float, lon: float) -> None:
        state = self._assign_jobs[job_id]
        state.state = "running"

        def progress(msg: str) -> None:
            state.processed += 1
            state.current = msg.strip()

        try:
            report = self._assign_fn(self._cfg, file_ids, lat, lon, progress=progress)
        except Exception as exc:  # surface any failure as a job error
            state.state = "error"
            state.error = str(exc)
            return

        state.assigned = report.assigned
        state.skipped = report.skipped
        state.place_string = report.place_string
        state.failures = list(report.failures)
        state.state = "done"

    # ----- rescan jobs (share the executor with organize/undo/retag) --------- #

    def submit_rescan(self) -> str:
        """Queue an index/disk reconciliation job and return its id (no cancel).

        Raises :class:`WorkerBusy` if another destructive job already holds the worker.
        """
        job_id = uuid.uuid4().hex
        with self._lock:
            busy = self._active_destructive_job()
            if busy is not None:
                raise WorkerBusy(busy)
            self._rescan_jobs[job_id] = RescanJobState(job_id=job_id)
        self._executor.submit(self._run_rescan, job_id)
        return job_id

    def rescan_status(self, job_id: str) -> RescanJobState | None:
        """Consistent point-in-time snapshot of a rescan job, or ``None`` if unknown."""
        with self._lock:
            state = self._rescan_jobs.get(job_id)
            return replace(state) if state is not None else None

    def _run_rescan(self, job_id: str) -> None:
        state = self._rescan_jobs[job_id]
        state.state = "running"

        def progress(msg: str) -> None:
            state.processed += 1
            state.current = msg.strip()

        try:
            report = self._rescan_fn(self._cfg, progress=progress)
        except Exception as exc:  # surface any failure as a job error
            state.state = "error"
            state.error = str(exc)
            return

        state.checked = report.checked
        state.pruned = report.pruned
        state.kept = report.kept
        state.warnings = list(report.warnings)
        state.orphaned = list(report.orphaned)
        state.state = "done"

    # ----- stitch jobs (dedicated pool — independent of the destructive one) -- #

    def submit_stitch(
        self, file_id: int, *, force: bool = False, projection: str | None = None
    ) -> str:
        """Queue a panorama stitch for ``file_id`` and return its id.

        Dedups: if a stitch for the same ``file_id`` is already pending/running its
        id is returned instead of starting a second ~7-min pass. A fresh job marks
        ``files.stitch_status='pending'`` so a concurrently-loaded GeoJSON reflects it.

        ``force`` re-runs the pipeline cold even when a fresh cached hero exists (the
        map UI's manual re-stitch); ``projection`` overrides the auto-detected Hugin
        projection (``'equirectangular'``/``'cylindrical'``/``'rectilinear'``).
        """
        with self._lock:
            for jid, st in self._stitch_jobs.items():
                if st.file_id == file_id and st.state in ("pending", "running"):
                    return jid
            job_id = uuid.uuid4().hex
            self._stitch_jobs[job_id] = StitchJobState(job_id=job_id, file_id=file_id)
        # Preserve an EXISTING good hero across a re-stitch: when the row is already 'ok'
        # we skip the 'pending' mark (a concurrent reload keeps showing the current hero)
        # and, on failure, leave the 'ok' row intact — the cached JPEG survives (same
        # preserve-on-failure discipline as restitch.py). This is keyed on the row being
        # 'ok', NOT on `force`: a first-time stitch (even with a projection override, which
        # also sets force) has no hero to protect, so it still marks 'pending' and records
        # 'failed' on failure.
        preserve = self._read_stitch_status(file_id) == "ok"
        if not preserve:
            self._mark_stitch_status(file_id, "pending")
        self._stitch_pool.submit(
            self._run_stitch, job_id, file_id,
            force=force, projection=projection, preserve=preserve,
        )
        return job_id

    def stitch_status(self, job_id: str) -> StitchJobState | None:
        """Consistent point-in-time snapshot of a stitch job, or ``None`` if unknown."""
        with self._lock:
            state = self._stitch_jobs.get(job_id)
            return replace(state) if state is not None else None

    def _read_stitch_status(self, file_id: int) -> str | None:
        """Current ``files.stitch_status`` for a panorama row (None if absent/cleared).

        Lets ``submit_stitch`` decide whether a re-stitch is protecting an existing 'ok'
        hero (preserve its row on failure) or is a first-time stitch (mark pending /
        record failed normally) — independent of the ``force`` cache-bypass flag."""
        conn = db.connect(self._cfg.index_db_path, integrity_check=False)
        try:
            row = conn.execute(
                "SELECT stitch_status FROM files WHERE id=? AND capture_kind='panorama'",
                (file_id,),
            ).fetchone()
            return row[0] if row is not None else None
        finally:
            conn.close()

    def _mark_stitch_status(
        self, file_id: int, status: str | None, projection: str | None = None
    ) -> None:
        """Write ``files.stitch_status`` for a panorama row (NULL clears it).

        When ``projection`` is given (only on a successful 'ok' stitch), the detected
        ``stitch_projection`` is written in the SAME UPDATE. The GeoJSON ETag folds in a
        ``stitch_projection`` code sum (alongside the ``stitch_status`` sum), so a
        conditional GET never 304s with a stale projection (m-fix-panorama-projection-
        autodetect)."""
        conn = db.connect(self._cfg.index_db_path, integrity_check=False)
        try:
            if projection is not None:
                conn.execute(
                    "UPDATE files SET stitch_status=?, stitch_projection=? "
                    "WHERE id=? AND capture_kind='panorama'",
                    (status, projection, file_id),
                )
            else:
                conn.execute(
                    "UPDATE files SET stitch_status=? WHERE id=? AND capture_kind='panorama'",
                    (status, file_id),
                )
            conn.commit()
        finally:
            conn.close()

    def _backfill_stitch_projection(
        self, file_id: int, cache_root, rel_key: str
    ) -> None:
        """Record a projection for a cached hero whose ``stitch_projection`` is NULL.

        Called only on a freshness cache HIT (the stitch returned no new HFOV). It fills
        the column from the cached hero's aspect (:func:`classify_stitched_projection`)
        ONLY when it was never recorded — never overwriting an authoritative cold-run
        value — so a cached flat hero that outlived its index-DB row (rebuild, or a crash
        between the cache write and the DB update) is not stuck in the 360 sphere viewer.
        The ``WHERE ... IS NULL`` makes it a no-op when a value already exists."""
        conn = db.connect(self._cfg.index_db_path, integrity_check=False)
        try:
            row = conn.execute(
                "SELECT stitch_projection FROM files "
                "WHERE id=? AND capture_kind='panorama'",
                (file_id,),
            ).fetchone()
            if row is None or row[0] is not None:
                return  # not a panorama, or already recorded — never overwrite
            out = stitch_cache_path(cache_root, rel_key)
            if not out.exists():
                return  # no cached hero to classify (shouldn't happen on a cache hit)
            conn.execute(
                "UPDATE files SET stitch_projection=? "
                "WHERE id=? AND capture_kind='panorama' AND stitch_projection IS NULL",
                (classify_stitched_projection(out), file_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _run_stitch(
        self, job_id: str, file_id: int, *, force: bool = False,
        projection: str | None = None, preserve: bool = False,
    ) -> None:
        state = self._stitch_jobs[job_id]
        state.state = "running"

        # Load the panorama primary tile + its frame-tile companions from the index.
        conn = db.connect(self._cfg.index_db_path, integrity_check=False)
        try:
            row = conn.execute(
                "SELECT dest_path, capture_kind FROM files WHERE id=?", (file_id,)
            ).fetchone()
            if row is None or row[1] != "panorama":
                state.state = "error"
                state.error = "not a panorama capture"
                return
            primary = _strip(row[0])
            # The stitched hero is cached under proxy_cache_dir keyed on the primary's
            # library-relative path — the SAME (cache_root, rel_key) the /api/stitch
            # serve route computes, so the generator and reader resolve one file.
            rel_key = pathing.library_rel_key(self._cfg.library_root, row[0])
            frames = [
                _strip(r[0])
                for r in conn.execute(
                    "SELECT dest_path FROM file_companions "
                    "WHERE primary_file_id=? AND companion_type='panorama_frame' "
                    "ORDER BY dest_path",
                    (file_id,),
                )
            ]
        finally:
            conn.close()

        proxy_cache_dir = config.resolve_proxy_cache_dir(self._cfg)

        def _on_step(index: int, total: int, name: str) -> None:
            state.step = index
            state.step_total = total
            state.step_name = name

        try:
            result = self._stitch_fn(
                proxy_cache_dir, rel_key, primary, frames,
                hugin_bin_dir=self._cfg.hugin_bin_dir,
                canvas=self._cfg.stitch_canvas,
                celeste=self._cfg.stitch_celeste,
                optimise_lens=self._cfg.stitch_optimise_lens,
                force=force,
                forced_projection=projection,
                on_step=_on_step,
            )
        except HuginNotFound:
            # Hugin absent: not a failure — clear back to NULL, keep the gallery.
            # When preserving an existing 'ok' hero (a re-stitch), leave the row untouched
            # so a retry that finds Hugin gone doesn't drop a still-valid hero.
            logger.warning(
                "panorama stitch for file_id=%s unavailable: Hugin not found "
                "(hugin_bin_dir=%s)", file_id, self._cfg.hugin_bin_dir,
            )
            if not preserve:
                self._mark_stitch_status(file_id, None)
            state.status = "unavailable"
            state.state = "done"
            return
        except StitchFailed as exc:
            # Preserve an existing hero on a re-stitch failure (the cached JPEG survives —
            # _stitch_gate/Hugin raise before _atomic_write); a first-time stitch records
            # 'failed' so the persistent failed badge + reload UI reflect it.
            if not preserve:
                self._mark_stitch_status(file_id, "failed")
            state.status = "failed"
            state.error = str(exc)
            state.state = "done"
            return
        except Exception as exc:  # unexpected — record failed + surface the error
            if not preserve:
                self._mark_stitch_status(file_id, "failed")
            state.state = "error"
            state.error = str(exc)
            return

        state.projection = result.projection
        if result.projection:
            # Cold run: the HFOV-derived projection is authoritative — record it.
            self._mark_stitch_status(file_id, "ok", result.projection)
        else:
            # Freshness cache hit (no new HFOV): mark ok, then BACKFILL the projection
            # from the cached hero's aspect ONLY if it was never recorded — e.g. the
            # cache survived an index-DB rebuild, or a crash landed between the cache
            # write and the DB update. This never overwrites a cold-run value, so a
            # cached flat hero is not left defaulting to the 360 sphere viewer.
            self._mark_stitch_status(file_id, "ok")
            self._backfill_stitch_projection(file_id, proxy_cache_dir, rel_key)
        state.status = "ok"
        state.state = "done"

    # ----- warm-pass jobs (dedicated pool — independent of the destructive one) - #

    def submit_warm(self, batch_id: str) -> str:
        """Queue a post-organize warm pass for ``batch_id`` and return its id.

        Runs on the dedicated ``_warm_pool`` (independent of the destructive and
        stitch pools), so it never blocks organize/undo/retag/rescan. Unguarded by
        :class:`WorkerBusy` — it is read-only cache regeneration.
        """
        job_id = uuid.uuid4().hex
        with self._lock:
            self._warm_jobs[job_id] = WarmJobState(job_id=job_id, batch_id=batch_id)
        self._warm_pool.submit(self._run_warm, job_id, batch_id)
        return job_id

    def warm_status(self, job_id: str) -> WarmJobState | None:
        """Consistent point-in-time snapshot of a warm job, or ``None`` if unknown."""
        with self._lock:
            state = self._warm_jobs.get(job_id)
            return replace(state) if state is not None else None

    def _run_warm(self, job_id: str, batch_id: str) -> None:
        state = self._warm_jobs[job_id]
        state.state = "running"

        def progress(name: str) -> None:
            state.processed += 1
            state.current = name

        try:
            result = self._warm_fn(self._cfg, batch_id, progress=progress)
        except Exception as exc:  # surface any failure as a job error
            state.state = "error"
            state.error = str(exc)
            return

        state.warmed = result.warmed
        state.proxies_warmed = result.proxies_warmed
        state.deleted = result.eviction.deleted
        state.skipped = result.eviction.skipped
        state.state = "done"
