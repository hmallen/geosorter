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


class JobManager:
    """Owns the worker pool and the live job table.

    ``organize_fn`` is injectable for tests; it defaults to the real
    :func:`geosorter.organize.run_organize` and is called as
    ``organize_fn(cfg, assume_yes=True, cancel=<predicate>, progress=<callback>)``.
    """

    def __init__(self, cfg, *, organize_fn=run_organize):
        self._cfg = cfg
        self._organize_fn = organize_fn
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._jobs: dict[str, JobState] = {}
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
