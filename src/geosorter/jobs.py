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

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace

from .organize import run_organize
from .retag import retag_file
from .undo import run_undo


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


class JobManager:
    """Owns the worker pool and the live job table.

    ``organize_fn``/``undo_fn``/``retag_fn`` are injectable for tests; they default
    to the real :func:`geosorter.organize.run_organize` /
    :func:`geosorter.undo.run_undo` / :func:`geosorter.retag.retag_file`. The single
    ``max_workers=1`` executor is shared by all three job kinds, so organize, undo,
    and re-tag can never run concurrently against the same library/index.
    """

    def __init__(self, cfg, *, organize_fn=run_organize, undo_fn=run_undo,
                 retag_fn=retag_file):
        self._cfg = cfg
        self._organize_fn = organize_fn
        self._undo_fn = undo_fn
        self._retag_fn = retag_fn
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._jobs: dict[str, JobState] = {}
        self._undo_jobs: dict[str, UndoJobState] = {}
        self._retag_jobs: dict[str, RetagJobState] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def submit(self) -> str:
        """Queue a new organize job and return its UUID job id."""
        job_id = uuid.uuid4().hex
        with self._lock:
            self._jobs[job_id] = JobState(job_id=job_id)
            self._cancels[job_id] = threading.Event()
        self._executor.submit(self._run, job_id)
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

    def _run(self, job_id: str) -> None:
        state = self._jobs[job_id]
        event = self._cancels[job_id]
        state.state = "running"

        def progress(msg: str) -> None:
            state.processed += 1
            state.current = msg.strip()

        try:
            report = self._organize_fn(
                self._cfg, assume_yes=True, cancel=event.is_set, progress=progress
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
