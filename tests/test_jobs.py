"""Tests for the background organize job manager (single-worker, cancellable)."""

from __future__ import annotations

import threading
import time

from geosorter.jobs import JobManager
from geosorter.organize import BatchReport


def _wait(mgr, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = mgr.status(job_id)
        if st is not None and st.state in ("done", "error", "cancelled"):
            return st
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish: {mgr.status(job_id)}")


def test_submit_runs_and_completes():
    def fake_organize(cfg, *, assume_yes, cancel, progress):
        progress("  DJI_0001.JPG")
        return BatchReport(batch_id="x", organized=3, quarantined=1, companions=2)

    mgr = JobManager(None, organize_fn=fake_organize)
    job_id = mgr.submit()
    assert isinstance(job_id, str) and len(job_id) >= 8
    st = _wait(mgr, job_id)
    assert st.state == "done"
    assert (st.organized, st.quarantined, st.companions) == (3, 1, 2)
    assert st.processed == 1  # progress callback advanced the counter


def test_status_unknown_returns_none():
    mgr = JobManager(None, organize_fn=lambda *a, **k: BatchReport(batch_id="x"))
    assert mgr.status("does-not-exist") is None


def test_cancel_sets_event_and_stops_between_groups():
    started = threading.Event()

    def fake_organize(cfg, *, assume_yes, cancel, progress):
        started.set()
        organized = 0
        for _ in range(1000):  # stand-in for the per-group loop
            if cancel():  # the manager's cancel Event is wired in here
                return BatchReport(batch_id="x", organized=organized, cancelled=True)
            organized += 1
            time.sleep(0.005)
        return BatchReport(batch_id="x", organized=organized)

    mgr = JobManager(None, organize_fn=fake_organize)
    job_id = mgr.submit()
    assert started.wait(2.0)
    assert mgr.cancel(job_id) is True
    st = _wait(mgr, job_id)
    assert st.state == "cancelled"
    assert st.organized < 1000  # stopped early


def test_cancel_unknown_job_returns_false():
    mgr = JobManager(None, organize_fn=lambda *a, **k: BatchReport(batch_id="x"))
    assert mgr.cancel("nope") is False


def test_pipeline_exception_becomes_error_state():
    def boom(cfg, *, assume_yes, cancel, progress):
        raise RuntimeError("kaboom")

    mgr = JobManager(None, organize_fn=boom)
    job_id = mgr.submit()
    st = _wait(mgr, job_id)
    assert st.state == "error"
    assert "kaboom" in st.error
