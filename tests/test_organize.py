"""Tests for the organize pipeline (scan → move → quarantine → report)."""

import os
import shutil
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from geosorter import db, geonames_loader, move_engine, organize
from geosorter.config import Config
from geosorter.metadata import MediaMetadata

FIXTURES = Path(__file__).parent / "fixtures" / "geonames"
MEDIA = Path(__file__).parent / "fixtures" / "media"


def _md(
    *,
    media_type="photo",
    lat=40.015,
    lon=-105.27,
    gps_source="exif",
    capture_ts_raw="2024:07:04 09:15:00",
    capture_ts_source_tag="EXIF:DateTimeOriginal",
    width=4000,
    height=3000,
    duration_s=None,
    codec=None,
):
    return MediaMetadata(
        media_type, lat, lon, gps_source, capture_ts_raw, capture_ts_source_tag,
        width, height, duration_s, codec, None, None, None,
    )


class _FakeExtractor:
    def __init__(self, mapping):
        self._mapping = mapping

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract(self, path):
        return self._mapping[Path(path).name]


def _factory(mapping):
    return lambda: _FakeExtractor(mapping)


def _setup(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    library = tmp_path / "library"
    gn_db = tmp_path / "geonames.db"
    geonames_loader.load(gn_db, FIXTURES, spatial_index="rtree")
    cfg = Config(
        inbox_path=inbox,
        library_root=library,
        index_db_path=tmp_path / "index.db",
        geonames_db_path=gn_db,
        spatial_index="rtree",
    )
    return cfg, inbox, library


def _add(inbox, name, data=b"capture-bytes"):
    p = inbox / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _index(cfg):
    return db.connect(cfg.index_db_path, integrity_check=False)


# --------------------------------------------------------------------------- #
def test_make_batch_id_and_first_run(tmp_path):
    assert organize.make_batch_id(datetime(2026, 5, 31, 12, 0, 0), "abc123") == "20260531T120000-abc123"
    cfg, inbox, _ = _setup(tmp_path)
    conn = db.connect(cfg.index_db_path, integrity_check=False)
    db.init_index_schema(conn)
    try:
        assert organize.is_first_run(conn) is True
    finally:
        conn.close()


def test_organize_photo_end_to_end(tmp_path):
    cfg, inbox, library = _setup(tmp_path)
    src = _add(inbox, "DJI_0001.JPG")
    report = organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0001.JPG": _md()})
    )
    assert report.organized == 1
    assert report.per_place == {"Boulder, Colorado, United States": 1}
    assert not src.exists()  # source auto-deleted
    conn = _index(cfg)
    try:
        frow = conn.execute("SELECT dest_path, status, local_date FROM files").fetchone()
        mrow = conn.execute("SELECT status FROM moves").fetchone()
    finally:
        conn.close()
    assert frow[1] == "organized"
    assert frow[2] == "2024-07-04"
    assert "Boulder, Colorado, United States" in frow[0]
    assert frow[0].endswith(r"2024-07-04\2024-07-04_09-15-00_DJI_0001.JPG")
    assert os.path.exists(organize._strip(frow[0]))
    assert mrow[0] == "source_deleted"


def test_byte_progress_forwarded(tmp_path):
    cfg, inbox, library = _setup(tmp_path)
    _add(inbox, "DJI_0001.JPG", data=b"capture-bytes" * 1000)
    ticks: list[tuple[str, str, int, int]] = []
    report = organize.run_organize(
        cfg,
        assume_yes=True,
        extractor_factory=_factory({"DJI_0001.JPG": _md()}),
        byte_progress=lambda name, phase, done, total: ticks.append((name, phase, done, total)),
    )
    assert report.organized == 1
    copying = [t for t in ticks if t[1] == "copying"]
    assert copying  # at least one copy tick was forwarded
    assert all(t[0] == "DJI_0001.JPG" for t in copying)  # filename carried through
    assert all(0 < done <= total for _n, _p, done, total in copying)


def test_organize_quarantines_no_gps(tmp_path):
    cfg, inbox, library = _setup(tmp_path)
    src = _add(inbox, "DJI_0009.JPG")
    report = organize.run_organize(
        cfg,
        assume_yes=True,
        extractor_factory=_factory({"DJI_0009.JPG": _md(lat=None, lon=None, gps_source="none")}),
    )
    assert report.quarantined == 1
    assert report.organized == 0
    assert not src.exists()
    conn = _index(cfg)
    try:
        frow = conn.execute("SELECT dest_path, status FROM files").fetchone()
    finally:
        conn.close()
    assert frow[1] == "quarantined"
    assert os.path.join("_no-gps", "2024-07-04", "DJI_0009.JPG") in organize._strip(frow[0])
    assert os.path.exists(organize._strip(frow[0]))


def test_neighbor_gps_inference_files_no_gps_capture(tmp_path):
    # A no-GPS photo shot 5 min after a GPS photo borrows its location (within the
    # default 30-min window); a no-GPS photo 10h later has no neighbor -> quarantine.
    cfg, inbox, library = _setup(tmp_path)
    _add(inbox, "DJI_0001.JPG", b"gps-capture")
    near = _add(inbox, "DJI_0002.JPG", b"near-no-gps")
    far = _add(inbox, "DJI_0003.JPG", b"far-no-gps")
    report = organize.run_organize(
        cfg,
        assume_yes=True,
        extractor_factory=_factory(
            {
                "DJI_0001.JPG": _md(capture_ts_raw="2024:07:04 09:15:00"),
                "DJI_0002.JPG": _md(
                    lat=None, lon=None, gps_source="none",
                    capture_ts_raw="2024:07:04 09:20:00",
                ),
                "DJI_0003.JPG": _md(
                    lat=None, lon=None, gps_source="none",
                    capture_ts_raw="2024:07:04 19:15:00",
                ),
            }
        ),
    )
    assert report.organized == 2  # the GPS photo + the inferred one
    assert report.inferred == 1
    assert report.quarantined == 1
    assert not near.exists() and not far.exists()
    conn = _index(cfg)
    try:
        inferred = conn.execute(
            "SELECT lat, lon, status, gps_source FROM files WHERE filename LIKE '%DJI_0002%'"
        ).fetchone()
        far_row = conn.execute(
            "SELECT status FROM files WHERE filename LIKE '%DJI_0003%'"
        ).fetchone()
    finally:
        conn.close()
    assert inferred[2] == "organized"
    assert inferred[3] == "inferred"
    assert inferred[0] == 40.015 and inferred[1] == -105.27
    assert far_row[0] == "quarantined"


def test_dedup_skips_identical_content(tmp_path):
    cfg, inbox, library = _setup(tmp_path)
    _add(inbox, "DJI_0001.JPG", b"same-content")
    organize.run_organize(cfg, assume_yes=True, extractor_factory=_factory({"DJI_0001.JPG": _md()}))
    # A different file (different name/path) with identical content = re-import dup.
    dup = _add(inbox, "DJI_0002.JPG", b"same-content")
    report = organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0002.JPG": _md()})
    )
    assert report.duplicates_skipped == 1
    assert report.organized == 0
    assert dup.exists()  # duplicate left in the inbox, NOT deleted
    conn = _index(cfg)
    try:
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
    finally:
        conn.close()


def test_collision_different_content_gets_suffix(tmp_path, monkeypatch):
    cfg, inbox, library = _setup(tmp_path)
    _add(inbox, "DJI_0001.JPG", b"content-A")
    _add(inbox, "DJI_0002.JPG", b"content-B")
    fixed = str(library / "Place" / "2024-07-04" / "2024-07-04_09-15-00_X.JPG")
    monkeypatch.setattr(organize.pathing, "compute_dest_path", lambda *a, **k: fixed)
    report = organize.run_organize(
        cfg,
        assume_yes=True,
        extractor_factory=_factory({"DJI_0001.JPG": _md(), "DJI_0002.JPG": _md()}),
    )
    assert report.organized == 2
    conn = _index(cfg)
    try:
        dests = {r[0] for r in conn.execute("SELECT dest_path FROM files")}
    finally:
        conn.close()
    assert fixed in dests
    assert any(d.endswith("_2.JPG") for d in dests)
    assert os.path.exists(fixed)


def test_codec_stats_written(tmp_path):
    cfg, inbox, library = _setup(tmp_path)
    _add(inbox, "DJI_0007.MP4", b"video-bytes")
    organize.run_organize(
        cfg,
        assume_yes=True,
        extractor_factory=_factory(
            {"DJI_0007.MP4": _md(media_type="video", codec="h265")}
        ),
    )
    conn = _index(cfg)
    try:
        row = conn.execute(
            "SELECT h264_count, h265_count, unknown_count FROM codec_stats"
        ).fetchone()
    finally:
        conn.close()
    assert row == (0, 1, 0)


def test_dry_run_writes_nothing(tmp_path):
    cfg, inbox, library = _setup(tmp_path)
    src = _add(inbox, "DJI_0001.JPG")
    report = organize.run_organize(
        cfg, dry_run=True, extractor_factory=_factory({"DJI_0001.JPG": _md()})
    )
    assert report.organized == 1
    assert src.exists()  # nothing moved
    assert not library.exists() or not any(library.rglob("*"))  # no files written
    conn = _index(cfg)
    try:
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM moves").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM geocode_cache").fetchone()[0] == 0
    finally:
        conn.close()


def test_first_run_gate_declined(tmp_path):
    cfg, inbox, library = _setup(tmp_path)
    src = _add(inbox, "DJI_0001.JPG")
    report = organize.run_organize(
        cfg,
        confirm=lambda _preview: False,
        extractor_factory=_factory({"DJI_0001.JPG": _md()}),
    )
    assert report.confirmed is False
    assert src.exists()
    conn = _index(cfg)
    try:
        assert conn.execute("SELECT COUNT(*) FROM moves").fetchone()[0] == 0
    finally:
        conn.close()


def test_group_atomic_companion_failure_keeps_primary(tmp_path, monkeypatch):
    cfg, inbox, library = _setup(tmp_path)
    primary = _add(inbox, "DJI_0003.MP4", b"video")
    companion = _add(inbox, "DJI_0003.SRT", b"telemetry")
    os.utime(companion, (primary.stat().st_atime, primary.stat().st_mtime))

    real = move_engine.copy_and_verify

    def _fail_srt(conn, batch, sp, dp, **kw):
        if str(sp).endswith(".SRT"):
            return move_engine.MoveOutcome("failed", "x", None, dp, "injected")
        return real(conn, batch, sp, dp, **kw)

    monkeypatch.setattr(organize.move_engine, "copy_and_verify", _fail_srt)
    report = organize.run_organize(
        cfg,
        assume_yes=True,
        extractor_factory=_factory({"DJI_0003.MP4": _md(media_type="video", codec="h264")}),
    )
    assert report.aborted is True
    assert primary.exists()  # group-atomic: primary source NOT deleted on companion failure
    assert companion.exists()


def test_disk_preflight_raises(tmp_path, monkeypatch):
    cfg, inbox, library = _setup(tmp_path)
    _add(inbox, "DJI_0001.JPG")
    monkeypatch.setattr(
        organize.shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(100, 100, 0)
    )
    with pytest.raises(OSError):
        organize.run_organize(
            cfg, assume_yes=True, extractor_factory=_factory({"DJI_0001.JPG": _md()})
        )


def test_freespace_abort_before_share_fills(tmp_path, monkeypatch):
    # Mid-run the share drops below the margin: organize aborts cleanly BETWEEN
    # groups (the in-flight group already filed atomically), leaving the rest in the
    # inbox rather than half-writing files until the disk is full.
    cfg, inbox, library = _setup(tmp_path)
    _add(inbox, "DJI_0001.JPG", b"first-capture")
    second = _add(inbox, "DJI_0002.JPG", b"second-capture")  # distinct bytes (no dedup)
    # Tiny files never reach the 1 GiB recheck cadence — recheck after every group.
    monkeypatch.setattr(organize, "_FREESPACE_RECHECK_BYTES", 1)
    calls = {"n": 0}

    def fake_disk_usage(_p):
        calls["n"] += 1
        # Call 1 is the preflight (plenty of room); every later call is a mid-run
        # recheck that now sees the share full.
        free = (1 << 50) if calls["n"] == 1 else 0
        return shutil._ntuple_diskusage(1 << 50, 1 << 50, free)

    monkeypatch.setattr(organize.shutil, "disk_usage", fake_disk_usage)
    report = organize.run_organize(
        cfg,
        assume_yes=True,
        extractor_factory=_factory({"DJI_0001.JPG": _md(), "DJI_0002.JPG": _md()}),
    )
    assert report.aborted is True
    assert report.organized == 1  # the first group was filed before the abort
    assert second.exists()  # the second group stayed in the inbox
    assert any("margin" in f for f in report.failures)


def test_sweeps_stale_partials_on_resume(tmp_path):
    # A crashed prior run can leave a `<dest>.partial` whose moves row is still
    # 'pending'. A fresh run sweeps those orphaned partials at start (reclaiming the
    # share) without walking the whole library.
    cfg, inbox, library = _setup(tmp_path)
    _add(inbox, "DJI_0001.JPG", b"capture")
    stale_dir = library / "Place" / "2024-07-04"
    stale_dir.mkdir(parents=True)
    partial = stale_dir / "old.JPG.partial"
    partial.write_bytes(b"stale-garbage")
    conn = db.connect(cfg.index_db_path)
    db.init_index_schema(conn)
    conn.execute(
        "INSERT INTO moves(batch_id, source_path, dest_path, source_sha256, status) "
        "VALUES ('oldbatch', '/gone/old.JPG', ?, 'deadbeef', 'pending')",
        (str(stale_dir / "old.JPG"),),
    )
    conn.commit()
    conn.close()
    assert partial.exists()

    organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0001.JPG": _md()})
    )
    assert not partial.exists()  # swept on resume


def test_extract_chunk_restart_resumes(tmp_path):
    # A daemon crash mid-extraction (extract raises) restarts a fresh extractor and
    # retries the file — the capture is extracted and filed, not lost.
    cfg, inbox, library = _setup(tmp_path)
    _add(inbox, "DJI_0001.JPG", b"capture")
    state = {"factory_calls": 0, "extract_calls": 0}

    class _FlakyExtractor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract(self, path):
            state["extract_calls"] += 1
            if state["extract_calls"] == 1:  # the daemon "crashes" once
                raise RuntimeError("exiftool daemon died")
            return _md()

    def factory():
        state["factory_calls"] += 1
        return _FlakyExtractor()

    report = organize.run_organize(cfg, assume_yes=True, extractor_factory=factory)
    assert report.organized == 1  # extracted on the retry and filed
    assert state["factory_calls"] >= 2  # the extractor was restarted at least once
    assert not (inbox / "DJI_0001.JPG").exists()


def test_unextractable_file_quarantined_after_max_failures(tmp_path):
    # A file ExifTool can never read (always raises) is given up on after
    # extract_max_failures, filed to the _no-gps quarantine, and reported — so one
    # corrupt file cannot abort the whole multi-hour pass.
    cfg, inbox, library = _setup(tmp_path)
    cfg = replace(cfg, extract_max_failures=2)
    good = _add(inbox, "DJI_0001.JPG", b"good-capture")
    bad = _add(inbox, "DJI_0002.JPG", b"corrupt-capture")
    attempts = {"bad": 0}

    class _PartlyBad:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract(self, path):
            if Path(path).name == "DJI_0002.JPG":
                attempts["bad"] += 1
                raise RuntimeError("unreadable / corrupt media")
            return _md()

    report = organize.run_organize(
        cfg, assume_yes=True, extractor_factory=lambda: _PartlyBad()
    )
    assert report.organized == 1  # the good capture still filed
    assert report.quarantined == 1  # the unreadable one quarantined, not lost
    assert attempts["bad"] == 2  # tried exactly extract_max_failures times
    assert any("DJI_0002.JPG" in f and "extraction failed" in f for f in report.failures)
    assert not good.exists() and not bad.exists()  # both left the inbox

    conn = _index(cfg)
    try:
        bad_row = conn.execute(
            "SELECT dest_path, status FROM files WHERE filename LIKE '%DJI_0002%'"
        ).fetchone()
    finally:
        conn.close()
    assert bad_row[1] == "quarantined"
    assert os.path.join("_no-gps", "") in organize._strip(bad_row[0])  # under _no-gps/


def test_verify_library_detects_bitrot(tmp_path):
    cfg, inbox, library = _setup(tmp_path)
    _add(inbox, "DJI_0001.JPG")
    organize.run_organize(cfg, assume_yes=True, extractor_factory=_factory({"DJI_0001.JPG": _md()}))

    clean = organize.verify_library(cfg)
    assert clean.checked == 1
    assert clean.ok == 1
    assert clean.mismatched == []

    conn = _index(cfg)
    try:
        dest = conn.execute("SELECT dest_path FROM files").fetchone()[0]
    finally:
        conn.close()
    with open(organize._strip(dest), "wb") as fh:
        fh.write(b"bit-rotted")
    rotted = organize.verify_library(cfg)
    assert rotted.mismatched == [organize._strip(dest)]


def test_partial_delete_recovers(tmp_path, monkeypatch):
    # Crash in Phase B *after* the companion source is deleted but *before* the
    # primary's — the primary stays the group-done sentinel and a re-run finishes
    # cleanly with no double-copy and no orphaned companion.
    cfg, inbox, library = _setup(tmp_path)
    primary = _add(inbox, "DJI_0003.MP4", b"video-bytes")
    companion = _add(inbox, "DJI_0003.SRT", b"telemetry")
    os.utime(companion, (primary.stat().st_atime, primary.stat().st_mtime))
    mapping = {"DJI_0003.MP4": _md(media_type="video", codec="h264")}

    real_delete = move_engine.commit_delete

    def _crash_before_primary(conn, sp, sha):
        if str(sp).endswith(".MP4"):  # primary is deleted last → crash just before it
            raise RuntimeError("simulated crash before primary delete")
        real_delete(conn, sp, sha)  # companion deletes normally

    monkeypatch.setattr(organize.move_engine, "commit_delete", _crash_before_primary)
    with pytest.raises(RuntimeError):
        organize.run_organize(cfg, assume_yes=True, extractor_factory=_factory(mapping))
    assert not companion.exists()  # companion source already deleted
    assert primary.exists()  # primary not yet deleted (crash before it)

    # Recovery run with the real delete restored.
    monkeypatch.setattr(organize.move_engine, "commit_delete", real_delete)
    report = organize.run_organize(cfg, assume_yes=True, extractor_factory=_factory(mapping))
    assert report.failures == [] and report.aborted is False
    assert not any(inbox.iterdir())  # both sources now gone
    conn = _index(cfg)
    try:
        deleted = conn.execute(
            "SELECT COUNT(*) FROM moves WHERE status='source_deleted'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert deleted == 2  # primary + companion, exactly once each
    assert organize.verify_library(cfg).ok == 2


def test_cancel_between_groups_leaves_remaining(tmp_path):
    # Cooperative cancel (checked between groups) finishes the in-flight group
    # atomically, marks the report cancelled, and leaves later captures untouched.
    cfg, inbox, library = _setup(tmp_path)
    first = _add(inbox, "DJI_0001.JPG", b"first")
    _add(inbox, "DJI_0002.JPG", b"second")
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 1  # allow the first group, cancel before the second

    report = organize.run_organize(
        cfg,
        assume_yes=True,
        cancel=cancel,
        extractor_factory=_factory({"DJI_0001.JPG": _md(), "DJI_0002.JPG": _md()}),
    )
    assert report.cancelled is True
    assert report.organized == 1
    assert not first.exists()  # first group fully organized
    assert sorted(p.name for p in inbox.iterdir()) == ["DJI_0002.JPG"]  # rest untouched


# --------------------------------------------------------------------------- #
# Hyperlapse handling (B10): a SRT-less render borrows its frames' GPS, the frames
# travel as hyperlapse_frame companions into a <render>_frames/ subfolder.
# --------------------------------------------------------------------------- #
def _hyperlapse_card(inbox, counter="0021", *, n_frames=3):
    """Build DCIM/DJI_001/<render>.MP4 + DCIM/HYPERLAPSE/001_<counter>/ frames.

    Returns ``(render_path, [frame_paths])``. The render mtime is set just after
    the frames, matching the real card (render written at capture end).
    """
    render = _add(
        inbox, f"DCIM/DJI_001/DJI_20240829183426_{counter}_D.MP4", b"render-bytes"
    )
    frames = []
    for i in range(1, n_frames + 1):
        f = _add(
            inbox,
            f"DCIM/HYPERLAPSE/001_{counter}/HYPERLAPSE_{i:04d}.JPG",
            f"frame-{i}".encode(),
        )
        os.utime(f, (1000.0 + i, 1000.0 + i))
        frames.append(f)
    os.utime(render, (1100.0, 1100.0))  # shortly after the frames
    return render, frames


def test_hyperlapse_borrows_frame_gps(tmp_path):
    cfg, inbox, library = _setup(tmp_path)
    render, frames = _hyperlapse_card(inbox, n_frames=3)
    mapping = {
        render.name: _md(media_type="video", lat=None, lon=None,
                         gps_source="none", codec="h264"),
        # Frames carry EXIF GPS (default _md lat/lon → Boulder fixture).
        "HYPERLAPSE_0001.JPG": _md(),
        "HYPERLAPSE_0002.JPG": _md(),
        "HYPERLAPSE_0003.JPG": _md(),
    }
    report = organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory(mapping)
    )
    assert report.organized == 1
    assert report.quarantined == 0
    assert report.companions == 3
    assert report.retained_frame_bytes > 0
    assert not render.exists() and not any(f.exists() for f in frames)

    conn = _index(cfg)
    try:
        frow = conn.execute(
            "SELECT dest_path, gps_source, capture_kind, frame_count FROM files"
        ).fetchone()
        ctypes = [
            r[0]
            for r in conn.execute(
                "SELECT companion_type FROM file_companions"
            ).fetchall()
        ]
    finally:
        conn.close()
    assert frow[1] == "hyperlapse_frame"
    assert frow[2] == "hyperlapse"
    assert frow[3] == 3
    assert ctypes == ["hyperlapse_frame"] * 3
    # Frames land in a <render_stem>_frames/ subfolder beside the render.
    dest = organize._strip(frow[0])
    frames_dir = Path(dest).with_suffix("").parent / (Path(dest).stem + "_frames")
    assert frames_dir.is_dir()
    assert sorted(p.name for p in frames_dir.iterdir()) == [
        "HYPERLAPSE_0001.JPG",
        "HYPERLAPSE_0002.JPG",
        "HYPERLAPSE_0003.JPG",
    ]


def test_hyperlapse_retain_false_files_render_only(tmp_path):
    cfg, inbox, library = _setup(tmp_path)
    cfg = replace(cfg, retain_hyperlapse_frames=False)
    render, frames = _hyperlapse_card(inbox, n_frames=3)
    mapping = {
        render.name: _md(media_type="video", lat=None, lon=None,
                         gps_source="none", codec="h264"),
        "HYPERLAPSE_0001.JPG": _md(),
        "HYPERLAPSE_0002.JPG": _md(),
        "HYPERLAPSE_0003.JPG": _md(),
    }
    report = organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory(mapping)
    )
    assert report.organized == 1
    assert report.companions == 0
    assert report.retained_frame_bytes == 0
    assert not render.exists()  # the render is still filed
    assert all(f.exists() for f in frames)  # frames left in the inbox

    conn = _index(cfg)
    try:
        frow = conn.execute(
            "SELECT gps_source, capture_kind, frame_count FROM files"
        ).fetchone()
        companions = conn.execute("SELECT COUNT(*) FROM file_companions").fetchone()[0]
    finally:
        conn.close()
    assert frow[0] == "hyperlapse_frame"  # GPS still borrowed from a frame
    assert frow[1] == "hyperlapse"
    assert frow[2] == 0
    assert companions == 0


def _panorama_card(inbox, counter="0002", *, n_frames=3):
    """Build DCIM/PANORAMA/001_<counter>/PANO_0001..N.JPG; return the tile paths."""
    tiles = []
    for i in range(1, n_frames + 1):
        t = _add(inbox, f"DCIM/PANORAMA/001_{counter}/PANO_{i:04d}.JPG", f"tile-{i}".encode())
        os.utime(t, (1000.0 + i, 1000.0 + i))
        tiles.append(t)
    return tiles


def test_panorama_files_as_capture_unit(tmp_path):
    cfg, inbox, library = _setup(tmp_path)
    tiles = _panorama_card(inbox, n_frames=4)
    # Every tile carries its own EXIF GPS (default _md → Boulder fixture); the
    # primary's coordinate is what the unit files under (gps_source='exif').
    mapping = {f"PANO_{i:04d}.JPG": _md() for i in range(1, 5)}
    report = organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory(mapping)
    )
    assert report.organized == 1
    assert report.quarantined == 0
    assert report.companions == 3
    assert not any(t.exists() for t in tiles)  # the whole unit moved

    conn = _index(cfg)
    try:
        frow = conn.execute(
            "SELECT dest_path, gps_source, capture_kind, frame_count FROM files"
        ).fetchone()
        ctypes = [
            r[0]
            for r in conn.execute("SELECT companion_type FROM file_companions").fetchall()
        ]
    finally:
        conn.close()
    assert frow[1] == "exif"  # GPS straight from the primary tile's EXIF
    assert frow[2] == "panorama"
    assert frow[3] == 3  # panorama_frame companions (tiles - 1)
    assert ctypes == ["panorama_frame"] * 3
    # The primary is PANO_0001; its tiles land in a <stem>_frames/ subfolder.
    dest = organize._strip(frow[0])
    assert Path(dest).name.endswith("PANO_0001.JPG")
    frames_dir = Path(dest).parent / (Path(dest).stem + "_frames")
    assert frames_dir.is_dir()
    assert sorted(p.name for p in frames_dir.iterdir()) == [
        "PANO_0002.JPG",
        "PANO_0003.JPG",
        "PANO_0004.JPG",
    ]


def _make_catalog(path, rows):
    """Build a minimal DJI-shaped MISC catalog DB at ``path``; ``rows`` = (file_name, star)."""
    import sqlite3
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE gis_info_table (file_name TEXT, star INT)")
    conn.executemany("INSERT INTO gis_info_table(file_name, star) VALUES (?,?)", rows)
    conn.execute("CREATE TABLE image_info_table (exif BLOB)")
    conn.execute("INSERT INTO image_info_table(exif) VALUES (?)", (b"\x00EXIF",))
    conn.commit()
    conn.close()
    return path


def test_catalog_ratings_applied_and_db_archived(tmp_path):
    cfg, inbox, library = _setup(tmp_path)
    _add(inbox, "DCIM/DJI_001/DJI_20240825165234_0001_D.MP4", b"video-bytes")
    catalog = _make_catalog(
        inbox / "MISC" / "FC.db",
        [("/mnt/media_rw/sdcard0/DCIM/DJI_001/DJI_20240825165234_0001_D.MP4", 4)],
    )
    mapping = {"DJI_20240825165234_0001_D.MP4": _md(media_type="video", codec="h264")}
    report = organize.run_organize(cfg, assume_yes=True, extractor_factory=_factory(mapping))

    assert report.organized == 1
    assert report.ratings_applied == 1
    assert report.unclaimed == 0  # the .db was archived, no longer stranded
    assert not catalog.exists()  # inbox copy archived away
    archive = Path(cfg.index_db_path).parent / "catalogs" / report.batch_id / "FC.db"
    assert archive.is_file()

    conn = _index(cfg)
    try:
        rating = conn.execute(
            "SELECT star_rating FROM files WHERE batch_id=? AND status='organized'",
            (report.batch_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert rating == 4


def test_catalog_stale_db_applies_no_ratings(tmp_path):
    # A catalog whose basenames match nothing organized this run -> no ratings, but
    # the .db is still archived (preserved + decluttered).
    cfg, inbox, library = _setup(tmp_path)
    _add(inbox, "DCIM/DJI_001/DJI_20240825165234_0001_D.MP4", b"video-bytes")
    catalog = _make_catalog(
        inbox / "MISC" / "stale.db",
        [("/mnt/media_rw/sdcard0/DCIM/100MEDIA/DJI_9999.MOV", 5)],
    )
    mapping = {"DJI_20240825165234_0001_D.MP4": _md(media_type="video", codec="h264")}
    report = organize.run_organize(cfg, assume_yes=True, extractor_factory=_factory(mapping))

    assert report.organized == 1
    assert report.ratings_applied == 0
    assert not catalog.exists()
    assert (Path(cfg.index_db_path).parent / "catalogs" / report.batch_id / "stale.db").is_file()

    conn = _index(cfg)
    try:
        rating = conn.execute(
            "SELECT star_rating FROM files WHERE batch_id=? AND status='organized'",
            (report.batch_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert rating is None  # never rated


def test_catalog_archive_oserror_does_not_abort(tmp_path, monkeypatch):
    # A catalog vanishing mid-archive (removable media) must NEVER abort post-move
    # bookkeeping: the media is already filed, ratings already applied, codec_stats
    # still written, and the failure is recorded rather than raised.
    cfg, inbox, library = _setup(tmp_path)
    _add(inbox, "DCIM/DJI_001/DJI_20240825165234_0001_D.MP4", b"video-bytes")
    _make_catalog(
        inbox / "MISC" / "FC.db",
        [("/mnt/media_rw/sdcard0/DCIM/DJI_001/DJI_20240825165234_0001_D.MP4", 4)],
    )
    mapping = {"DJI_20240825165234_0001_D.MP4": _md(media_type="video", codec="h264")}

    real_copy = organize.move_engine.copy_and_verify

    def boom(conn, batch_id, src, dst, **kw):
        if str(src).lower().endswith(".db"):
            raise FileNotFoundError("catalog vanished mid-run")
        return real_copy(conn, batch_id, src, dst, **kw)

    monkeypatch.setattr(organize.move_engine, "copy_and_verify", boom)
    report = organize.run_organize(cfg, assume_yes=True, extractor_factory=_factory(mapping))

    assert report.organized == 1  # the media still filed
    assert report.ratings_applied == 1  # ratings applied before the archive failed
    assert any("catalog archive error" in f for f in report.failures)
    assert not report.aborted
    conn = _index(cfg)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM codec_stats WHERE batch_id=?", (report.batch_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1  # post-move bookkeeping completed


def test_prescan_warnings_surface_in_report(tmp_path):
    # A non-.db MISC cache file (THM thumbnail, unclaimed) + an orphan HYPERLAPSE dir
    # (no matching render) must be reported, never silently dropped or quarantined.
    # (A MISC .db would be archived by B11, so it would NOT stay unclaimed.)
    cfg, inbox, library = _setup(tmp_path)
    _add(inbox, "MISC/THM/THM0001.thm", b"thumb")
    orphan = _add(
        inbox, "DCIM/HYPERLAPSE/001_0099/HYPERLAPSE_0001.JPG", b"orphan-frame"
    )
    report = organize.run_organize(cfg, assume_yes=True, extractor_factory=_factory({}))
    assert report.organized == 0
    assert report.quarantined == 0
    assert report.unclaimed == 1  # the MISC THM cache file (left in the inbox)
    assert any("0099" in w for w in report.warnings)  # orphan hyperlapse dir
    assert orphan.exists()  # left in the inbox, not moved


# --------------------------------------------------------------------------- #
# Import selection — run_organize(selected_primaries=...) filters capture groups.
# --------------------------------------------------------------------------- #
def test_selected_primaries_filters_groups(tmp_path):
    cfg, inbox, library = _setup(tmp_path)
    _add(inbox, "DJI_0001.JPG", b"capture-one")
    _add(inbox, "DJI_0002.JPG", b"capture-two")  # distinct bytes (avoid dedup)
    mapping = {"DJI_0001.JPG": _md(), "DJI_0002.JPG": _md()}
    report = organize.run_organize(
        cfg,
        assume_yes=True,
        selected_primaries={"DJI_0001.JPG"},
        extractor_factory=_factory(mapping),
    )
    assert report.organized == 1
    assert not (inbox / "DJI_0001.JPG").exists()  # selected -> filed
    assert (inbox / "DJI_0002.JPG").exists()  # not selected -> left in inbox


def test_selected_primaries_none_organizes_all(tmp_path):
    cfg, inbox, library = _setup(tmp_path)
    _add(inbox, "DJI_0001.JPG", b"capture-one")
    _add(inbox, "DJI_0002.JPG", b"capture-two")  # distinct bytes (avoid dedup)
    mapping = {"DJI_0001.JPG": _md(), "DJI_0002.JPG": _md()}
    report = organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory(mapping)
    )
    assert report.organized == 2


def test_partial_import_preserves_catalog(tmp_path):
    # On a partial import (a subset selected) star ratings are still applied to the
    # selected files, and the MISC catalog is PRESERVE-COPIED (byte-for-byte) into the
    # archive so its data survives a card pull — but the inbox copy is KEPT (not the
    # destructive move) so a later import can still apply ratings to the rest.
    import hashlib

    cfg, inbox, library = _setup(tmp_path)
    _add(inbox, "DCIM/DJI_001/DJI_20240825165234_0001_D.MP4", b"video-bytes")
    _add(inbox, "DCIM/DJI_001/DJI_20240825165234_0002_D.MP4", b"video2-bytes")
    catalog = _make_catalog(
        inbox / "MISC" / "FC.db",
        [("/mnt/media_rw/sdcard0/DCIM/DJI_001/DJI_20240825165234_0001_D.MP4", 4)],
    )
    mapping = {
        "DJI_20240825165234_0001_D.MP4": _md(media_type="video", codec="h264"),
        "DJI_20240825165234_0002_D.MP4": _md(media_type="video", codec="h264"),
    }
    report = organize.run_organize(
        cfg,
        assume_yes=True,
        selected_primaries={"DCIM/DJI_001/DJI_20240825165234_0001_D.MP4"},
        extractor_factory=_factory(mapping),
    )
    assert report.organized == 1
    assert report.ratings_applied == 1  # ratings still applied to the selected file
    assert catalog.exists()  # inbox copy KEPT (not destructively archived)
    assert (inbox / "DCIM/DJI_001/DJI_20240825165234_0002_D.MP4").exists()
    # ...but a byte-identical preserve-copy now lives in the archive.
    archive = Path(cfg.index_db_path).parent / "catalogs" / report.batch_id / "FC.db"
    assert archive.is_file()
    assert (
        hashlib.sha256(archive.read_bytes()).hexdigest()
        == hashlib.sha256(catalog.read_bytes()).hexdigest()
    )
    assert report.unclaimed == 1  # the .db is still stranded in the inbox


# --------------------------------------------------------------------------- #
# Real-ExifTool end-to-end (Phase 0a DoD). Uses the actual MetadataExtractor.
# --------------------------------------------------------------------------- #
def test_committed_mixed_footage_e2e(tmp_path):
    # Real extractor + real move engine on committed fixtures: a GPS photo
    # organizes, a no-GPS video quarantines.
    cfg, inbox, library = _setup(tmp_path)
    shutil.copy(MEDIA / "dji_photo.jpg", inbox / "DJI_0001.JPG")  # EXIF GPS -> organized
    shutil.copy(MEDIA / "h264_tiny.mp4", inbox / "DJI_0002.MP4")  # no GPS -> quarantined
    report = organize.run_organize(cfg, assume_yes=True)  # default MetadataExtractor
    assert report.organized == 1
    assert report.quarantined == 1
    assert not (inbox / "DJI_0001.JPG").exists()
    assert not (inbox / "DJI_0002.MP4").exists()
    conn = _index(cfg)
    try:
        statuses = {
            r[0]: r[1]
            for r in conn.execute("SELECT media_type, status FROM files")
        }
        moves = conn.execute("SELECT COUNT(*) FROM moves WHERE status='source_deleted'").fetchone()[0]
    finally:
        conn.close()
    assert statuses.get("photo") == "organized"
    assert statuses.get("video") == "quarantined"
    assert moves == 2

    verify = organize.verify_library(cfg)
    assert verify.checked == 2 and verify.ok == 2


def test_real_dji_footage_e2e(tmp_path, local_media):
    # Richer DoD on real gitignored originals; skips cleanly when absent.
    # dji_h265_nogps.mp4 has no EMBEDDED GPS but its .SRT sidecar supplies it, so
    # B2's SRT fallback recovers GPS and it organizes (not quarantines) — and the
    # .SRT rides along as a companion.
    gps_video = local_media("dji_h264_gps.mp4")
    srt_video = local_media("dji_h265_nogps.mp4")
    srt_sidecar = local_media("dji_h265_nogps.SRT")
    cfg, inbox, library = _setup(tmp_path)
    shutil.copy(MEDIA / "dji_photo.jpg", inbox / "DJI_0001.JPG")
    shutil.copy(gps_video, inbox / "DJI_0002.MP4")
    shutil.copy(srt_video, inbox / "DJI_0003.MP4")
    shutil.copy(srt_sidecar, inbox / "DJI_0003.SRT")
    report = organize.run_organize(cfg, assume_yes=True)
    assert report.organized == 3  # photo + embedded-GPS video + SRT-GPS video
    assert report.companions >= 1  # the .SRT rode along with DJI_0003.MP4
    assert report.aborted is False and report.failures == []
    assert not any(inbox.iterdir())  # every source moved out of the inbox
    conn = _index(cfg)
    try:
        moved = conn.execute(
            "SELECT COUNT(*) FROM moves WHERE status='source_deleted'"
        ).fetchone()[0]
        companions = conn.execute("SELECT COUNT(*) FROM file_companions").fetchone()[0]
    finally:
        conn.close()
    assert moved == 4  # 3 primaries + 1 .SRT companion
    assert companions >= 1
    assert organize.verify_library(cfg).ok == 4
