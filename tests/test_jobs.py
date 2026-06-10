"""Tests for the background organize job manager (single-worker, cancellable)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from geosorter import db
from geosorter.derived import HuginNotFound, StitchFailed
from geosorter.jobs import JobManager, WorkerBusy, _compute_eta
from geosorter.organize import BatchReport
from geosorter.rescan import RescanReport
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
                      selected_primaries=None, on_plan=None):
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
                      selected_primaries=None, on_plan=None):
        byte_progress("DJI_0003.MP4", "copying", 5, 10)
        return BatchReport(batch_id="x", organized=1)

    mgr = JobManager(None, organize_fn=fake_organize)
    st = _wait(mgr, mgr.submit())
    assert st.state == "done"
    assert st.current == "DJI_0003.MP4"
    assert st.current_phase == "copying"
    assert (st.bytes_done, st.bytes_total) == (5, 10)


def _wait_warm(mgr, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = mgr.warm_status(job_id)
        if st is not None and st.state in ("done", "error"):
            return st
        time.sleep(0.01)
    raise AssertionError(f"warm job {job_id} did not finish: {mgr.warm_status(job_id)}")


def test_warm_job_lifecycle():
    from geosorter.derived import EvictionResult
    from geosorter.warm import WarmResult

    calls = []

    def fake_warm(cfg, batch_id, *, progress=None, cancel=None):
        calls.append(batch_id)
        progress("p.jpg")
        return WarmResult(batch_id=batch_id, warmed=3,
                          eviction=EvictionResult(0, 0, deleted=2, skipped=1))

    mgr = JobManager(None, warm_fn=fake_warm)
    job_id = mgr.submit_warm("b9")
    st = _wait_warm(mgr, job_id)
    assert st.state == "done"
    assert st.batch_id == "b9"
    assert (st.warmed, st.deleted, st.skipped) == (3, 2, 1)
    assert st.processed == 1
    assert calls == ["b9"]


def test_organize_completion_triggers_warm():
    # A successful organize (organized>0) auto-enqueues a warm pass for its batch.
    from geosorter.derived import EvictionResult
    from geosorter.warm import WarmResult

    fired = threading.Event()
    seen = {}

    def fake_organize(cfg, *, assume_yes, cancel, progress, byte_progress,
                      selected_primaries=None, on_plan=None):
        return BatchReport(batch_id="bX", organized=1)

    def fake_warm(cfg, batch_id, *, progress=None, cancel=None):
        seen["batch_id"] = batch_id
        fired.set()
        return WarmResult(batch_id=batch_id, warmed=0, eviction=EvictionResult(0, 0, 0, 0))

    mgr = JobManager(None, organize_fn=fake_organize, warm_fn=fake_warm)
    _wait(mgr, mgr.submit())
    assert fired.wait(2.0)
    assert seen["batch_id"] == "bX"


def test_organize_with_nothing_organized_skips_warm():
    fired = threading.Event()

    def fake_organize(cfg, *, assume_yes, cancel, progress, byte_progress,
                      selected_primaries=None, on_plan=None):
        return BatchReport(batch_id="bY", organized=0)

    def fake_warm(cfg, batch_id, *, progress=None, cancel=None):
        fired.set()
        from geosorter.derived import EvictionResult
        from geosorter.warm import WarmResult
        return WarmResult(batch_id=batch_id, warmed=0, eviction=EvictionResult(0, 0, 0, 0))

    mgr = JobManager(None, organize_fn=fake_organize, warm_fn=fake_warm)
    _wait(mgr, mgr.submit())
    assert not fired.wait(0.5)  # organized==0 -> no warm pass


def test_compute_eta():
    # Pure ETA helper: seconds remaining at the observed byte rate.
    assert _compute_eta(1000, 0, 5.0) is None  # nothing done yet → not estimable
    assert _compute_eta(0, 0, 5.0) is None  # nothing to do
    assert _compute_eta(1000, 500, 5.0) == pytest.approx(5.0)  # half in 5s → ~5s left
    assert _compute_eta(1000, 1000, 5.0) == 0.0  # done → 0 remaining


def test_status_exposes_plan_and_eta():
    # run_organize reports the plan totals via on_plan; the job exposes total_groups,
    # total_bytes, a running bytes_done_total, and a bytes-based eta_seconds.
    def fake_organize(cfg, *, assume_yes, cancel, progress, byte_progress,
                      selected_primaries=None, on_plan=None):
        if on_plan is not None:
            on_plan(10, 1000)
        byte_progress("DJI_0001.MP4", "copying", 500, 1000)
        return BatchReport(batch_id="x", organized=1)

    mgr = JobManager(None, organize_fn=fake_organize)
    st = _wait(mgr, mgr.submit())
    assert st.state == "done"
    assert st.total_groups == 10
    assert st.total_bytes == 1000
    assert st.eta_seconds == 0.0  # a finished job reports zero remaining, not a stale guess


def test_status_unknown_returns_none():
    mgr = JobManager(None, organize_fn=lambda *a, **k: BatchReport(batch_id="x"))
    assert mgr.status("does-not-exist") is None


def test_cancel_sets_event_and_stops_between_groups():
    started = threading.Event()

    def fake_organize(cfg, *, assume_yes, cancel, progress, byte_progress,
                      selected_primaries=None, on_plan=None):
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
             selected_primaries=None, on_plan=None):
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


def test_destructive_submits_raise_workerbusy_during_organize():
    # undo/retag/rescan share the single destructive worker with organize; submitting
    # one while organize is in flight must raise WorkerBusy (the route maps it to 409)
    # carrying the blocking organize job id — never a silent queue behind a long run.
    block = threading.Event()

    def slow_organize(cfg, *, assume_yes, cancel, progress, byte_progress,
                      selected_primaries=None, on_plan=None):
        block.wait(2.0)
        return BatchReport(batch_id="x")

    mgr = JobManager(None, organize_fn=slow_organize,
                     undo_fn=lambda *a, **k: UndoReport(),
                     retag_fn=lambda *a, **k: RetagReport(),
                     rescan_fn=lambda *a, **k: RescanReport())
    org_id = mgr.submit()
    try:
        for call in (mgr.submit_undo, lambda: mgr.submit_retag(1, 0.0, 0.0),
                     mgr.submit_rescan):
            with pytest.raises(WorkerBusy) as ei:
                call()
            assert ei.value.blocking_job_id == org_id
    finally:
        block.set()
    _wait(mgr, org_id)  # drain so the worker is free for other tests


def test_organize_submit_not_blocked_by_active_job():
    # Organize-submit is deliberately NOT guarded (the map UI's Process-Inbox flow):
    # a second organize queues behind the first rather than 409-ing.
    block = threading.Event()

    def slow_organize(cfg, *, assume_yes, cancel, progress, byte_progress,
                      selected_primaries=None, on_plan=None):
        block.wait(2.0)
        return BatchReport(batch_id="x")

    mgr = JobManager(None, organize_fn=slow_organize)
    first = mgr.submit()
    second = mgr.submit()  # must NOT raise — queues behind the first
    assert second != first
    block.set()
    _wait(mgr, first)
    _wait(mgr, second)


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


# --------------------------------------------------------------------------- #
def _wait_rescan(mgr, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = mgr.rescan_status(job_id)
        if st is not None and st.state in ("done", "error"):
            return st
        time.sleep(0.01)
    raise AssertionError(f"rescan job {job_id} did not finish: {mgr.rescan_status(job_id)}")


def test_submit_rescan_runs_and_completes():
    def fake_rescan(cfg, *, dry_run=False, progress):
        progress("  2024-07-04_09-15-00_DJI_0001.JPG")
        return RescanReport(
            checked=3, kept=2, pruned=1, warnings=["w"], orphaned=["/x/y.SRT"]
        )

    mgr = JobManager(None, rescan_fn=fake_rescan)
    job_id = mgr.submit_rescan()
    st = _wait_rescan(mgr, job_id)
    assert st.state == "done"
    assert st.checked == 3
    assert st.kept == 2
    assert st.pruned == 1
    assert st.warnings == ["w"]
    assert st.orphaned == ["/x/y.SRT"]
    assert st.processed == 1


def test_rescan_status_unknown_returns_none():
    mgr = JobManager(None, rescan_fn=lambda *a, **k: RescanReport())
    assert mgr.rescan_status("does-not-exist") is None


def test_rescan_exception_becomes_error_state():
    def boom(cfg, *, dry_run=False, progress):
        raise RuntimeError("rescan-boom")

    mgr = JobManager(None, rescan_fn=boom)
    job_id = mgr.submit_rescan()
    st = _wait_rescan(mgr, job_id)
    assert st.state == "error"
    assert "rescan-boom" in st.error


# --------------------------------------------------------------------------- #
# Panorama stitch jobs (B13) — run on a dedicated pool, write files.stitch_status.
def _wait_stitch(mgr, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = mgr.stitch_status(job_id)
        if st is not None and st.state in ("done", "error"):
            return st
        time.sleep(0.01)
    raise AssertionError(f"stitch job {job_id} did not finish: {mgr.stitch_status(job_id)}")


def _build_index_with_panorama(tmp_path):
    """Create a real index DB with one panorama primary + 2 frame-tile companions."""
    lib = tmp_path / "lib"
    (lib / "pano").mkdir(parents=True)
    primary = lib / "pano" / "PANO_0001.JPG"
    primary.write_bytes(b"x")
    frames = []
    for i in (2, 3):
        f = lib / "pano" / f"PANO_000{i}.JPG"
        f.write_bytes(b"x")
        frames.append(f)
    idx = tmp_path / "index.db"
    conn = db.connect(idx)
    db.init_index_schema(conn)
    cur = conn.execute(
        "INSERT INTO files (dest_path, filename, media_type, sha256, status, "
        "capture_kind, frame_count) VALUES (?, 'PANO_0001.JPG', 'photo', 'h', "
        "'organized', 'panorama', 3)",
        (str(primary),),
    )
    file_id = cur.lastrowid
    for f in frames:
        conn.execute(
            "INSERT INTO file_companions (primary_file_id, dest_path, companion_type) "
            "VALUES (?, ?, 'panorama_frame')",
            (file_id, str(f)),
        )
    conn.commit()
    conn.close()
    cfg = SimpleNamespace(
        index_db_path=idx, library_root=lib, hugin_bin_dir=None, proxy_cache_dir=None,
        stitch_canvas="4000x2000", stitch_celeste=True, stitch_optimise_lens=True,
    )
    return cfg, file_id, primary, frames


def _read_stitch_status(cfg, file_id):
    conn = db.connect(cfg.index_db_path, integrity_check=False)
    try:
        return conn.execute(
            "SELECT stitch_status FROM files WHERE id=?", (file_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def test_submit_stitch_success_writes_ok(tmp_path):
    cfg, file_id, primary, frames = _build_index_with_panorama(tmp_path)
    seen = {}

    def fake_stitch(cache_root, rel_key, prim, frms, *, hugin_bin_dir, on_step=None, **_):
        seen["cache_root"] = cache_root
        seen["rel_key"] = rel_key
        seen["primary"] = prim
        seen["frames"] = list(frms)
        return Path("stitched.jpg")

    mgr = JobManager(cfg, stitch_fn=fake_stitch)
    job_id = mgr.submit_stitch(file_id)
    st = _wait_stitch(mgr, job_id)
    assert st.state == "done"
    assert st.status == "ok"
    assert _read_stitch_status(cfg, file_id) == "ok"
    # the job loaded the primary + frame tiles from the DB and passed them through,
    # plus the proxy cache root (None -> library_root) + the primary's library-rel key.
    assert seen["primary"] == str(primary)
    assert seen["frames"] == [str(f) for f in frames]
    assert seen["cache_root"] == cfg.library_root  # proxy_cache_dir None -> library_root
    assert seen["rel_key"] == "pano/PANO_0001.JPG"  # collision-free, generator==reader


def test_submit_stitch_reports_step_progress(tmp_path):
    # The job snapshot reflects the live Hugin step reported via on_step, so the
    # map UI can show "step 3/6: cpclean" during the multi-minute run.
    cfg, file_id, _, _ = _build_index_with_panorama(tmp_path)

    def stepping_stitch(cache_root, rel_key, prim, frms, *, hugin_bin_dir, on_step, **_):
        on_step(3, 6, "cpclean")
        return Path("stitched.jpg")

    mgr = JobManager(cfg, stitch_fn=stepping_stitch)
    st = _wait_stitch(mgr, mgr.submit_stitch(file_id))
    assert st.state == "done"
    assert st.step == 3
    assert st.step_total == 6
    assert st.step_name == "cpclean"


def test_submit_stitch_failed_writes_failed(tmp_path):
    cfg, file_id, _, _ = _build_index_with_panorama(tmp_path)

    def boom(cache_root, rel_key, prim, frms, *, hugin_bin_dir, on_step=None, **_):
        raise StitchFailed("cpfind lost the sky")

    mgr = JobManager(cfg, stitch_fn=boom)
    st = _wait_stitch(mgr, mgr.submit_stitch(file_id))
    assert st.state == "done"
    assert st.status == "failed"
    assert _read_stitch_status(cfg, file_id) == "failed"


def test_submit_stitch_unavailable_when_hugin_missing(tmp_path):
    cfg, file_id, _, _ = _build_index_with_panorama(tmp_path)

    def no_hugin(cache_root, rel_key, prim, frms, *, hugin_bin_dir, on_step=None, **_):
        raise HuginNotFound("no hugin")

    mgr = JobManager(cfg, stitch_fn=no_hugin)
    st = _wait_stitch(mgr, mgr.submit_stitch(file_id))
    assert st.state == "done"
    assert st.status == "unavailable"
    # Hugin absent -> the row keeps NULL (no hero), not 'failed'
    assert _read_stitch_status(cfg, file_id) is None


def test_stitch_status_unknown_returns_none(tmp_path):
    cfg, _, _, _ = _build_index_with_panorama(tmp_path)
    mgr = JobManager(cfg, stitch_fn=lambda *a, **k: Path("x.jpg"))
    assert mgr.stitch_status("does-not-exist") is None


def test_submit_stitch_dedups_inflight_and_marks_pending(tmp_path):
    cfg, file_id, _, _ = _build_index_with_panorama(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def blocking_stitch(cache_root, rel_key, prim, frms, *, hugin_bin_dir, on_step=None, **_):
        entered.set()
        release.wait(2.0)
        return Path("stitched.jpg")

    mgr = JobManager(cfg, stitch_fn=blocking_stitch)
    j1 = mgr.submit_stitch(file_id)
    assert entered.wait(2.0)  # first stitch is now running
    assert _read_stitch_status(cfg, file_id) == "pending"  # pending while in-flight
    j2 = mgr.submit_stitch(file_id)  # same file, in-flight -> dedup
    assert j2 == j1
    release.set()
    st = _wait_stitch(mgr, j1)
    assert st.status == "ok"
    assert _read_stitch_status(cfg, file_id) == "ok"


def test_stitch_pool_independent_of_destructive_pool(tmp_path):
    # A 7-min read-only stitch must NOT wait behind a destructive organize job:
    # the stitch finishes while the organize worker is still held busy.
    cfg, file_id, _, _ = _build_index_with_panorama(tmp_path)
    org_block = threading.Event()

    def slow_organize(cfg, *, assume_yes, cancel, progress, byte_progress,
                      selected_primaries=None, on_plan=None):
        org_block.wait(3.0)  # occupy the destructive single worker
        return BatchReport(batch_id="x")

    mgr = JobManager(cfg, organize_fn=slow_organize,
                     stitch_fn=lambda *a, **k: Path("x.jpg"))
    mgr.submit()  # fills the destructive pool
    st = _wait_stitch(mgr, mgr.submit_stitch(file_id))  # must complete anyway
    assert st.status == "ok"
    org_block.set()
