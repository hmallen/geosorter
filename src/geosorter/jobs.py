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
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace

from . import db
from .derived import HuginNotFound, StitchFailed, panorama_stitch
from .organize import run_organize
from .rescan import run_rescan
from .retag import retag_file
from .undo import run_undo

logger = logging.getLogger("geosorter.jobs")


def _strip(dest_path: str) -> str:
    """Drop the Windows ``\\\\?\\`` long-path prefix if present."""
    return dest_path[4:] if dest_path.startswith("\\\\?\\") else dest_path


@dataclass
class JobState:
    """Serializable snapshot of one organize job's progress."""

    job_id: str
    state: str = "pending"  # pending | running | done | error | cancelled
    organized: int = 0
    quarantined: int = 0
    duplicates_skipped: int = 0
    companions: int = 0
    processed: int = 0  # files seen so far (progress callback ticks)
    current: str | None = None  # most recent file being processed
    current_phase: str | None = None  # 'hashing' | 'copying' | 'verifying' (byte progress)
    bytes_done: int = 0  # bytes processed in the current file's current phase
    bytes_total: int = 0  # current file's size (0 until a byte-progress tick arrives)
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
                 retag_fn=retag_file, rescan_fn=run_rescan, stitch_fn=panorama_stitch):
        self._cfg = cfg
        self._organize_fn = organize_fn
        self._undo_fn = undo_fn
        self._retag_fn = retag_fn
        self._rescan_fn = rescan_fn
        self._stitch_fn = stitch_fn
        self._executor = ThreadPoolExecutor(max_workers=1)
        # A panorama stitch is read-only (~7 min) and strictly off the crash-safe
        # move path, so it gets its OWN single worker: stitches serialize among
        # themselves but never block (or wait behind) organize/undo/retag.
        self._stitch_pool = ThreadPoolExecutor(max_workers=1)
        self._jobs: dict[str, JobState] = {}
        self._undo_jobs: dict[str, UndoJobState] = {}
        self._retag_jobs: dict[str, RetagJobState] = {}
        self._rescan_jobs: dict[str, RescanJobState] = {}
        self._stitch_jobs: dict[str, StitchJobState] = {}
        self._cancels: dict[str, threading.Event] = {}
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
            return replace(state) if state is not None else None

    def cancel(self, job_id: str) -> bool:
        """Request cancellation; returns ``False`` for an unknown job id."""
        event = self._cancels.get(job_id)
        if event is None:
            return False
        event.set()
        return True

    def _run(self, job_id: str, selected_primaries: set[str] | None = None) -> None:
        state = self._jobs[job_id]
        event = self._cancels[job_id]
        state.state = "running"

        def progress(msg: str) -> None:
            state.processed += 1
            state.current = msg.strip()

        def byte_progress(name: str, phase: str, done: int, total: int) -> None:
            state.current = name
            state.current_phase = phase
            state.bytes_done = done
            state.bytes_total = total

        try:
            report = self._organize_fn(
                self._cfg, assume_yes=True, cancel=event.is_set,
                progress=progress, byte_progress=byte_progress,
                selected_primaries=selected_primaries,
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

    # ----- undo jobs (share the executor + cancel table with organize) ------- #

    def submit_undo(self, batch_id: str | None = None) -> str:
        """Queue a new undo job (most recent batch by default) and return its id."""
        job_id = uuid.uuid4().hex
        with self._lock:
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
        """Queue a manual re-tag of ``file_id`` to ``(lat, lon)`` and return its id."""
        job_id = uuid.uuid4().hex
        with self._lock:
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

    # ----- rescan jobs (share the executor with organize/undo/retag) --------- #

    def submit_rescan(self) -> str:
        """Queue an index/disk reconciliation job and return its id (no cancel)."""
        job_id = uuid.uuid4().hex
        with self._lock:
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

    def submit_stitch(self, file_id: int) -> str:
        """Queue a panorama stitch for ``file_id`` and return its id.

        Dedups: if a stitch for the same ``file_id`` is already pending/running its
        id is returned instead of starting a second ~7-min pass. A fresh job marks
        ``files.stitch_status='pending'`` so a concurrently-loaded GeoJSON reflects it.
        """
        with self._lock:
            for jid, st in self._stitch_jobs.items():
                if st.file_id == file_id and st.state in ("pending", "running"):
                    return jid
            job_id = uuid.uuid4().hex
            self._stitch_jobs[job_id] = StitchJobState(job_id=job_id, file_id=file_id)
        self._mark_stitch_status(file_id, "pending")
        self._stitch_pool.submit(self._run_stitch, job_id, file_id)
        return job_id

    def stitch_status(self, job_id: str) -> StitchJobState | None:
        """Consistent point-in-time snapshot of a stitch job, or ``None`` if unknown."""
        with self._lock:
            state = self._stitch_jobs.get(job_id)
            return replace(state) if state is not None else None

    def _mark_stitch_status(self, file_id: int, status: str | None) -> None:
        """Write ``files.stitch_status`` for a panorama row (NULL clears it)."""
        conn = db.connect(self._cfg.index_db_path, integrity_check=False)
        try:
            conn.execute(
                "UPDATE files SET stitch_status=? WHERE id=? AND capture_kind='panorama'",
                (status, file_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _run_stitch(self, job_id: str, file_id: int) -> None:
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

        def _on_step(index: int, total: int, name: str) -> None:
            state.step = index
            state.step_total = total
            state.step_name = name

        try:
            self._stitch_fn(
                self._cfg.library_root, primary, frames,
                hugin_bin_dir=self._cfg.hugin_bin_dir, on_step=_on_step,
            )
        except HuginNotFound:
            # Hugin absent: not a failure — clear back to NULL, keep the gallery.
            logger.warning(
                "panorama stitch for file_id=%s unavailable: Hugin not found "
                "(hugin_bin_dir=%s)", file_id, self._cfg.hugin_bin_dir,
            )
            self._mark_stitch_status(file_id, None)
            state.status = "unavailable"
            state.state = "done"
            return
        except StitchFailed as exc:
            self._mark_stitch_status(file_id, "failed")
            state.status = "failed"
            state.error = str(exc)
            state.state = "done"
            return
        except Exception as exc:  # unexpected — record failed + surface the error
            self._mark_stitch_status(file_id, "failed")
            state.state = "error"
            state.error = str(exc)
            return

        self._mark_stitch_status(file_id, "ok")
        state.status = "ok"
        state.state = "done"
