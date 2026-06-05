"""Tests for the background organize job manager (single-worker, cancellable)."""

from __future__ import annotations

import threading
import time

from geosorter.jobs import JobManager
from geosorter.organize import BatchReport
from geosorter.retag import RetagReport
from geosorter.undo import UndoReport


def _wait(mgr, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = mgr.status(job_id)
        if st is not None and st.state in ("done", "error", "cancelled"):
            return st
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish: {mgr.status(job_id)}")


def _wait_undo(mgr, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = mgr.undo_status(job_id)
        if st is not None and st.state in ("done", "error", "cancelled"):
            return st
        time.sleep(0.01)
    raise AssertionError(f"undo job {job_id} did not finish: {mgr.undo_status(job_id)}")


def test_submit_runs_and_completes():
    def fake_organize(cfg, *, assume_yes, cancel, progress, byte_progress,
                      selected_primaries=None):
        progress("  DJI_0001.JPG")
        return BatchReport(batch_id="x", organized=3, quarantined=1, companions=2)

    mgr = JobManager(None, organize_fn=fake_organize)
    job_id = mgr.submit()
    assert isinstance(job_id, str) and len(job_id) >= 8
    st = _wait(mgr, job_id)
    assert st.state == "done"
    assert (st.organized, st.quarantined, st.companions) == (3, 1, 2)
    assert st.processed == 1  # progress callback advanced the counter


def test_status_exposes_byte_progress_fields():
    def fake_organize(cfg, *, assume_yes, cancel, progress, byte_progress,
                      selected_primaries=None):
        byte_progress("DJI_0003.MP4", "copying", 5, 10)
        return BatchReport(batch_id="x", organized=1)

    mgr = JobManager(None, organize_fn=fake_organize)
    st = _wait(mgr, mgr.submit())
    assert st.state == "done"
    assert st.current == "DJI_0003.MP4"
    assert st.current_phase == "copying"
    assert (st.bytes_done, st.bytes_total) == (5, 10)


def test_status_unknown_returns_none():
    mgr = JobManager(None, organize_fn=lambda *a, **k: BatchReport(batch_id="x"))
    assert mgr.status("does-not-exist") is None


def test_cancel_sets_event_and_stops_between_groups():
    started = threading.Event()

    def fake_organize(cfg, *, assume_yes, cancel, progress, byte_progress,
                      selected_primaries=None):
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
    def boom(cfg, *, assume_yes, cancel, progress, byte_progress,
             selected_primaries=None):
        raise RuntimeError("kaboom")

    mgr = JobManager(None, organize_fn=boom)
    job_id = mgr.submit()
    st = _wait(mgr, job_id)
    assert st.state == "error"
    assert "kaboom" in st.error


# --------------------------------------------------------------------------- #
def test_submit_undo_runs_and_completes():
    def fake_undo(cfg, *, batch_id, cancel, progress):
        progress("  DJI_0001.JPG")
        return UndoReport(batch_id="b1", restored=3, conflicts=["/inbox/x.jpg"])

    mgr = JobManager(None, undo_fn=fake_undo)
    job_id = mgr.submit_undo()
    st = _wait_undo(mgr, job_id)
    assert st.state == "done"
    assert st.restored == 3
    assert st.batch_id == "b1"
    assert st.conflicts == ["/inbox/x.jpg"]
    assert st.processed == 1


def test_undo_status_unknown_returns_none():
    mgr = JobManager(None, undo_fn=lambda *a, **k: UndoReport())
    assert mgr.undo_status("does-not-exist") is None


def test_undo_nothing_to_undo_maps_to_done():
    mgr = JobManager(None, undo_fn=lambda *a, **k: UndoReport(nothing_to_undo=True))
    job_id = mgr.submit_undo()
    st = _wait_undo(mgr, job_id)
    assert st.state == "done"
    assert st.nothing_to_undo is True
    assert st.restored == 0


def test_undo_cancel_sets_event_and_stops():
    started = threading.Event()

    def fake_undo(cfg, *, batch_id, cancel, progress):
        started.set()
        restored = 0
        for _ in range(1000):
            if cancel():
                return UndoReport(batch_id="b1", restored=restored, cancelled=True)
            restored += 1
            time.sleep(0.005)
        return UndoReport(batch_id="b1", restored=restored)

    mgr = JobManager(None, undo_fn=fake_undo)
    job_id = mgr.submit_undo()
    assert started.wait(2.0)
    assert mgr.cancel(job_id) is True
    st = _wait_undo(mgr, job_id)
    assert st.state == "cancelled"
    assert st.restored < 1000


def test_undo_exception_becomes_error_state():
    def boom(cfg, *, batch_id, cancel, progress):
        raise RuntimeError("undo-boom")

    mgr = JobManager(None, undo_fn=boom)
    job_id = mgr.submit_undo()
    st = _wait_undo(mgr, job_id)
    assert st.state == "error"
    assert "undo-boom" in st.error


# --------------------------------------------------------------------------- #
def _wait_retag(mgr, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = mgr.retag_status(job_id)
        if st is not None and st.state in ("done", "error"):
            return st
        time.sleep(0.01)
    raise AssertionError(f"retag job {job_id} did not finish: {mgr.retag_status(job_id)}")


def test_submit_retag_runs_and_completes():
    def fake_retag(cfg, file_id, lat, lon, *, progress):
        progress("  2024-07-04_09-15-00_DJI_0001.JPG")
        return RetagReport(
            file_id=file_id, status="retagged", moved=2,
            place_string="Denver, Colorado, United States",
        )

    mgr = JobManager(None, retag_fn=fake_retag)
    job_id = mgr.submit_retag(7, 39.7, -104.9)
    st = _wait_retag(mgr, job_id)
    assert st.state == "done"
    assert st.status == "retagged"
    assert st.moved == 2
    assert st.place_string == "Denver, Colorado, United States"
    assert st.processed == 1


def test_retag_status_unknown_returns_none():
    mgr = JobManager(None, retag_fn=lambda *a, **k: RetagReport())
    assert mgr.retag_status("does-not-exist") is None


def test_retag_not_found_maps_to_done():
    mgr = JobManager(None, retag_fn=lambda *a, **k: RetagReport(status="not_found"))
    job_id = mgr.submit_retag(999, 0.0, 0.0)
    st = _wait_retag(mgr, job_id)
    assert st.state == "done"
    assert st.status == "not_found"
    assert st.moved == 0


def test_retag_exception_becomes_error_state():
    def boom(cfg, file_id, lat, lon, *, progress):
        raise RuntimeError("retag-boom")

    mgr = JobManager(None, retag_fn=boom)
    job_id = mgr.submit_retag(1, 0.0, 0.0)
    st = _wait_retag(mgr, job_id)
    assert st.state == "error"
    assert "retag-boom" in st.error
