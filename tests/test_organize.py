"""Tests for the organize pipeline (scan → move → quarantine → report)."""

import hashlib
import json
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


def test_organize_invalidates_filed_dest_cache(tmp_path):
    # (m-fix-stale-derived-cache-thumbnails) organize files content at a dest path that
    # may have been used by DIFFERENT prior content (recover / recycled-name re-file /
    # re-import after undo). The optional `invalidate` callback is called per moved dest
    # so any stale derived asset there is cleared. organize.py stays Pillow-free — the
    # callback is injected by the caller (jobs.py wires it to derived.invalidate).
    cfg, inbox, library = _setup(tmp_path)
    _add(inbox, "DJI_0001.JPG")
    calls = []
    report = organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0001.JPG": _md()}),
        invalidate=lambda dest: calls.append(dest),
    )
    assert report.organized == 1
    assert any("Boulder" in d and d.endswith("DJI_0001.JPG") for d in calls)


def test_recycled_name_across_dirs_files_both(tmp_path):
    # Regression for the recycled-DJI-filename data-loss bug: two UNRELATED files with
    # the SAME stem in DIFFERENT inbox subdirs (DJI counters recycle per SD card) must
    # BOTH be filed to distinct destinations, both indexed, and neither overwritten.
    # Pre-fix they merged into one capture group → one filed, one filed as an 'other'
    # companion onto the SAME dest → overwrite + both sources deleted = silent loss.
    cfg, inbox, _library = _setup(tmp_path)
    _add(inbox, "cardA/DJI_0001.JPG", data=b"AAAA-real-2024")
    _add(inbox, "cardB/DJI_0001.JPG", data=b"BBBB-stray-2023")

    md_a = _md(capture_ts_raw="2024:07:04 09:15:00")
    md_b = _md(capture_ts_raw="2023:07:07 18:14:33")

    class _PathExtractor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract(self, path):
            return md_a if Path(path).parent.name == "cardA" else md_b

    report = organize.run_organize(cfg, assume_yes=True, extractor_factory=lambda: _PathExtractor())

    assert report.organized == 2
    assert report.duplicates_skipped == 0
    conn = _index(cfg)
    try:
        dests = [r[0] for r in conn.execute("SELECT dest_path FROM files").fetchall()]
        companions = conn.execute("SELECT count(*) FROM file_companions").fetchone()[0]
    finally:
        conn.close()
    assert len(dests) == 2
    assert len(set(dests)) == 2  # two DISTINCT destinations, not one
    assert companions == 0  # neither file became the other's (self-colliding) companion
    for d in dests:
        assert os.path.exists(organize._strip(d))
    # Both capture's bytes survive on disk — nothing overwritten.
    on_disk = {Path(organize._strip(d)).read_bytes() for d in dests}
    assert on_disk == {b"AAAA-real-2024", b"BBBB-stray-2023"}


def test_byte_progress_forwarded(tmp_path, monkeypatch):
    # Byte-level copy progress is a copy-path feature; force cross-volume so the copy
    # path (not the same-volume rename, which moves no bytes) runs.
    monkeypatch.setattr(organize, "_same_volume", lambda a, b: False)
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
    # Default relocation moved the dup out of its inbox path into _duplicates/.
    assert not dup.exists()
    assert (inbox / "_duplicates" / "DJI_0002.JPG").exists()
    conn = _index(cfg)
    try:
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
    finally:
        conn.close()


def test_duplicate_relocated_and_logged(tmp_path):
    # Default relocate_duplicates: a re-imported duplicate is MOVED into
    # <inbox>/_duplicates/<relpath> and recorded in _duplicates/duplicates.log with the
    # incoming path and the matched library path.
    cfg, inbox, _library = _setup(tmp_path)
    _add(inbox, "DJI_0001.JPG", b"same-content")
    organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0001.JPG": _md()})
    )
    dup = _add(inbox, "sub/DJI_0002.JPG", b"same-content")
    report = organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0002.JPG": _md()})
    )
    assert report.duplicates_skipped == 1
    assert report.duplicates_relocated == 1
    assert not dup.exists()  # moved out of its inbox path
    moved = inbox / "_duplicates" / "sub" / "DJI_0002.JPG"  # subpath preserved
    assert moved.exists() and moved.read_bytes() == b"same-content"

    log = (inbox / "_duplicates" / "duplicates.log").read_text(encoding="utf-8")
    conn = _index(cfg)
    try:
        matched = conn.execute(
            "SELECT dest_path FROM files WHERE status='organized'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert str(dup) in log  # incoming path
    assert organize._strip(matched) in log  # matched library path


def test_duplicate_relocation_off_leaves_in_place(tmp_path):
    cfg, inbox, _library = _setup(tmp_path)
    cfg = replace(cfg, relocate_duplicates=False)
    _add(inbox, "DJI_0001.JPG", b"same-content")
    organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0001.JPG": _md()})
    )
    dup = _add(inbox, "DJI_0002.JPG", b"same-content")
    report = organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0002.JPG": _md()})
    )
    assert report.duplicates_skipped == 1
    assert report.duplicates_relocated == 0
    assert dup.exists()  # left in place
    assert not (inbox / "_duplicates").exists()


def test_duplicate_relocation_moves_companion(tmp_path):
    cfg, inbox, _library = _setup(tmp_path)
    _add(inbox, "DJI_0001.JPG", b"prim-content")
    organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0001.JPG": _md()})
    )
    # Re-import the same primary content plus a .DNG companion, in a different dir.
    prim = _add(inbox, "card/DJI_0001.JPG", b"prim-content")
    dng = _add(inbox, "card/DJI_0001.DNG", b"raw-content")
    report = organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0001.JPG": _md()})
    )
    assert report.duplicates_relocated == 1
    assert not prim.exists() and not dng.exists()  # whole group moved together
    assert (inbox / "_duplicates" / "card" / "DJI_0001.JPG").exists()
    assert (inbox / "_duplicates" / "card" / "DJI_0001.DNG").exists()


def test_relocated_duplicates_excluded_from_scan(tmp_path):
    from geosorter import grouping
    from geosorter import inbox as inbox_mod

    cfg, inbox, _library = _setup(tmp_path)
    _add(inbox, "DJI_0001.JPG", b"dup")
    organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0001.JPG": _md()})
    )
    _add(inbox, "DJI_0002.JPG", b"dup")
    organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0002.JPG": _md()})
    )
    # The relocated file under _duplicates/ must be invisible to every inbox scan.
    assert all("_duplicates" not in p.relative_to(inbox).parts for p in grouping.scan_inbox_files(inbox))
    assert inbox_mod.count_inbox(inbox).captures == 0


def test_relocated_duplicate_suffixes_on_collision(tmp_path):
    # A second duplicate landing at the same _duplicates/ relpath must not clobber the
    # first — it is suffixed (recycled DJI names across cards).
    cfg, inbox, _library = _setup(tmp_path)
    _add(inbox, "DJI_0001.JPG", b"X")
    organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0001.JPG": _md()})
    )
    _add(inbox, "d/DJI_0002.JPG", b"X")
    organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0002.JPG": _md()})
    )
    assert (inbox / "_duplicates" / "d" / "DJI_0002.JPG").exists()
    # Same inbox relpath, again a duplicate → relocation must suffix, not overwrite.
    _add(inbox, "d/DJI_0002.JPG", b"X")
    organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0002.JPG": _md()})
    )
    assert (inbox / "_duplicates" / "d" / "DJI_0002.JPG").exists()
    assert (inbox / "_duplicates" / "d" / "DJI_0002_2.JPG").exists()


def test_duplicate_relocation_off_records_pending_row(tmp_path):
    # relocate_duplicates off: the skipped duplicate stays in the inbox AND is
    # persisted in the duplicates table (source, hash, companions, matched library
    # row) so the review panel can see and drain the backlog.
    cfg, inbox, _library = _setup(tmp_path)
    cfg = replace(cfg, relocate_duplicates=False)
    _add(inbox, "DJI_0001.JPG", b"same-content")
    organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0001.JPG": _md()})
    )
    prim = _add(inbox, "card/DJI_0002.JPG", b"same-content")
    dng = _add(inbox, "card/DJI_0002.DNG", b"raw-bytes")
    report = organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0002.JPG": _md()})
    )
    assert report.duplicates_skipped == 1
    assert prim.exists()  # left in place, only recorded
    conn = _index(cfg)
    try:
        rows = conn.execute(
            "SELECT source_path, sha256, companion_paths, matched_file_id, "
            "matched_dest_path, batch_id, first_seen_at FROM duplicates"
        ).fetchall()
        matched = conn.execute(
            "SELECT id, dest_path FROM files WHERE status='organized'"
        ).fetchone()
    finally:
        conn.close()
    assert len(rows) == 1
    src, sha, companions_json, mid, mdest, batch, first_seen = rows[0]
    assert src == str(prim)
    assert sha == hashlib.sha256(b"same-content").hexdigest()
    assert json.loads(companions_json) == [str(dng)]
    assert mid == matched[0]
    assert mdest == organize._strip(matched[1])
    assert batch == report.batch_id
    assert first_seen  # stamped by the table default


def test_duplicate_record_is_idempotent_across_runs(tmp_path):
    # A second run re-detects the same inbox duplicate: the row is upserted
    # (same id, batch refreshed), never duplicated.
    cfg, inbox, _library = _setup(tmp_path)
    cfg = replace(cfg, relocate_duplicates=False)
    _add(inbox, "DJI_0001.JPG", b"same-content")
    organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0001.JPG": _md()})
    )
    _add(inbox, "DJI_0002.JPG", b"same-content")
    factory = _factory({"DJI_0002.JPG": _md()})
    organize.run_organize(cfg, assume_yes=True, extractor_factory=factory)
    conn = _index(cfg)
    try:
        first_id = conn.execute("SELECT id FROM duplicates").fetchone()[0]
    finally:
        conn.close()
    second = organize.run_organize(cfg, assume_yes=True, extractor_factory=factory)
    assert second.duplicates_skipped == 1
    conn = _index(cfg)
    try:
        rows = conn.execute("SELECT id, batch_id FROM duplicates").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0][0] == first_id  # upsert kept the row (and its first_seen_at)
    assert rows[0][1] == second.batch_id  # ...but refreshed the volatile fields


def test_duplicate_relocation_on_prunes_pending_row(tmp_path):
    # With relocation ON the physical move to _duplicates/ is the record: no row
    # is left behind, and a stale row from a relocate-off era is pruned.
    cfg, inbox, _library = _setup(tmp_path)
    off = replace(cfg, relocate_duplicates=False)
    _add(inbox, "DJI_0001.JPG", b"dup-bytes")
    organize.run_organize(
        off, assume_yes=True, extractor_factory=_factory({"DJI_0001.JPG": _md()})
    )
    _add(inbox, "DJI_0002.JPG", b"dup-bytes")
    factory = _factory({"DJI_0002.JPG": _md()})
    organize.run_organize(off, assume_yes=True, extractor_factory=factory)
    conn = _index(cfg)
    try:
        assert conn.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0] == 1
    finally:
        conn.close()
    # Flip relocation on (the _setup default): the dup moves out and the row dies.
    report = organize.run_organize(cfg, assume_yes=True, extractor_factory=factory)
    assert report.duplicates_relocated == 1
    conn = _index(cfg)
    try:
        assert conn.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0] == 0
    finally:
        conn.close()


def test_import_after_undo_prunes_stale_duplicate_row(tmp_path):
    # When the matched library file is undone (files row gone), the former
    # duplicate imports normally on the next run — and its stale pending row
    # must be pruned with it.
    cfg, inbox, _library = _setup(tmp_path)
    cfg = replace(cfg, relocate_duplicates=False)
    _add(inbox, "DJI_0001.JPG", b"undone-bytes")
    organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0001.JPG": _md()})
    )
    _add(inbox, "DJI_0002.JPG", b"undone-bytes")
    factory = _factory({"DJI_0002.JPG": _md()})
    organize.run_organize(cfg, assume_yes=True, extractor_factory=factory)
    conn = _index(cfg)
    try:
        assert conn.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0] == 1
        # Undo-style deletion of the matched library rows: the sha match is gone
        # (real undo removes the moves rows with the files rows).
        conn.execute("DELETE FROM moves")
        conn.execute("DELETE FROM files")
        conn.commit()
    finally:
        conn.close()
    report = organize.run_organize(cfg, assume_yes=True, extractor_factory=factory)
    assert report.organized == 1
    conn = _index(cfg)
    try:
        assert conn.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0] == 0
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
    # Group-atomic copy semantics (verify all, then delete) are copy-path only; force
    # cross-volume so copy_and_verify is the move primitive being failed here.
    monkeypatch.setattr(organize, "_same_volume", lambda a, b: False)
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
    # The disk preflight only runs on the copy path (a rename adds no net bytes);
    # force cross-volume so the preflight is exercised.
    monkeypatch.setattr(organize, "_same_volume", lambda a, b: False)
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
    # The mid-run free-space recheck only runs on the copy path; force cross-volume.
    monkeypatch.setattr(organize, "_same_volume", lambda a, b: False)
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
    # Phase B (verify-all-then-delete) is copy-path only; the rename path has no
    # separate commit_delete to crash in. Force cross-volume for this scenario.
    monkeypatch.setattr(organize, "_same_volume", lambda a, b: False)
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


def test_same_volume_uses_rename(tmp_path, monkeypatch):
    # On one volume, organize moves each file by atomic rename: copy_and_verify is
    # never called, no `.partial` is ever staged, and the move records source_deleted
    # with the source hash stored (so undo / dedup / verify-library still work).
    monkeypatch.setattr(organize, "_same_volume", lambda a, b: True)
    cfg, inbox, library = _setup(tmp_path)
    primary = _add(inbox, "DJI_0003.MP4", b"video-bytes")
    companion = _add(inbox, "DJI_0003.SRT", b"telemetry")
    os.utime(companion, (primary.stat().st_atime, primary.stat().st_mtime))

    def _no_copy(*a, **k):
        raise AssertionError("copy_and_verify must not run on the same-volume path")

    monkeypatch.setattr(organize.move_engine, "copy_and_verify", _no_copy)
    report = organize.run_organize(
        cfg,
        assume_yes=True,
        extractor_factory=_factory({"DJI_0003.MP4": _md(media_type="video", codec="h264")}),
    )
    assert report.organized == 1
    assert report.companions == 1
    assert not primary.exists() and not companion.exists()  # renamed away, no copy
    assert not list(library.rglob("*.partial"))  # nothing staged
    conn = _index(cfg)
    try:
        frow = conn.execute("SELECT dest_path, status FROM files").fetchone()
        statuses = [r[0] for r in conn.execute("SELECT status FROM moves").fetchall()]
        shas = conn.execute(
            "SELECT source_sha256, dest_sha256 FROM moves WHERE source_path=?",
            (str(primary),),
        ).fetchone()
    finally:
        conn.close()
    assert frow[1] == "organized"
    assert os.path.exists(organize._strip(frow[0]))  # file present at dest
    assert statuses == ["source_deleted", "source_deleted"]  # primary + companion
    assert shas[0] is not None and shas[0] == shas[1]  # source hash stored, dest == source


def test_cross_volume_uses_copy(tmp_path, monkeypatch):
    # Different volumes → the copy+verify path is used (rename would raise EXDEV).
    monkeypatch.setattr(organize, "_same_volume", lambda a, b: False)
    cfg, inbox, library = _setup(tmp_path)
    primary = _add(inbox, "DJI_0001.JPG", b"capture-bytes")
    calls = {"n": 0}
    real = move_engine.copy_and_verify

    def _spy(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(organize.move_engine, "copy_and_verify", _spy)
    report = organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0001.JPG": _md()})
    )
    assert report.organized == 1
    assert calls["n"] >= 1  # the copy path was exercised
    assert not primary.exists()
    conn = _index(cfg)
    try:
        mrow = conn.execute("SELECT status FROM moves").fetchone()
    finally:
        conn.close()
    assert mrow[0] == "source_deleted"


def test_same_volume_partial_group_resume(tmp_path, monkeypatch):
    # A crash after a companion is renamed but before the primary: the companion is at
    # the dest, the primary still in the inbox. A re-run completes the group with no
    # double-move and no FileNotFoundError (the primary is moved last, so it is still
    # present to be re-read for its dedup hash on resume).
    monkeypatch.setattr(organize, "_same_volume", lambda a, b: True)
    cfg, inbox, library = _setup(tmp_path)
    primary = _add(inbox, "DJI_0003.MP4", b"video-bytes")
    companion = _add(inbox, "DJI_0003.SRT", b"telemetry")
    os.utime(companion, (primary.stat().st_atime, primary.stat().st_mtime))
    mapping = {"DJI_0003.MP4": _md(media_type="video", codec="h264")}

    real_rename = move_engine.rename_in_place
    state = {"fail_primary": True}

    def _flaky(conn, batch, sp, dp, **k):
        if state["fail_primary"] and str(sp).endswith(".MP4"):
            return move_engine.MoveOutcome("failed", None, None, dp, "simulated crash")
        return real_rename(conn, batch, sp, dp, **k)

    monkeypatch.setattr(organize.move_engine, "rename_in_place", _flaky)
    r1 = organize.run_organize(cfg, assume_yes=True, extractor_factory=_factory(mapping))
    assert r1.aborted is True
    assert not companion.exists()  # companion renamed first (away)
    assert primary.exists()  # primary not yet moved

    state["fail_primary"] = False
    r2 = organize.run_organize(cfg, assume_yes=True, extractor_factory=_factory(mapping))
    assert r2.aborted is False and r2.failures == []
    assert r2.organized == 1
    assert not primary.exists() and not companion.exists()
    conn = _index(cfg)
    try:
        nfiles = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        ndel = conn.execute(
            "SELECT COUNT(*) FROM moves WHERE status='source_deleted'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert nfiles == 1  # one primary files row, not duplicated
    assert ndel == 2  # primary + companion, each exactly once
    assert organize.verify_library(cfg).ok == 2  # both files hash-verify at dest


def test_same_volume_persists_before_primary_rename(tmp_path, monkeypatch):
    # Crash-safety invariant (fixes the orphan window): the `files` row must be durable
    # BEFORE the primary's source is renamed away, because the primary's source_deleted
    # is the group-done sentinel Pass 1 skips on. If the rename happened first, a crash
    # between it and the index write would leave a file in the library that no
    # map/undo/rescan knows about. Assert a `files` row already exists at the moment the
    # primary is renamed.
    monkeypatch.setattr(organize, "_same_volume", lambda a, b: True)
    cfg, inbox, library = _setup(tmp_path)
    primary = _add(inbox, "DJI_0003.MP4", b"video-bytes")
    companion = _add(inbox, "DJI_0003.SRT", b"telemetry")
    os.utime(companion, (primary.stat().st_atime, primary.stat().st_mtime))

    real_rename = move_engine.rename_in_place
    seen: dict[str, int] = {}

    def _checking(conn, batch, sp, dp, **k):
        if str(sp).endswith(".MP4"):  # the primary, moved last
            seen["files_rows"] = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        return real_rename(conn, batch, sp, dp, **k)

    monkeypatch.setattr(organize.move_engine, "rename_in_place", _checking)
    report = organize.run_organize(
        cfg,
        assume_yes=True,
        extractor_factory=_factory({"DJI_0003.MP4": _md(media_type="video", codec="h264")}),
    )
    assert report.organized == 1
    assert seen.get("files_rows") == 1  # the files row existed BEFORE the primary moved


def test_finalize_renamed_pending_reconciles_orphan(tmp_path, monkeypatch):
    # Simulate a crash INSIDE the primary's rename (os.replace done, status commit not):
    # a 'pending' moves row whose source is gone + dest present, plus a durable files row
    # (persisted before the crash). The inbox-driven regroup can't reconcile it (source
    # gone), so a subsequent organize run must finalize it from the move log to
    # source_deleted — otherwise undo would later mishandle the pending row.
    monkeypatch.setattr(organize, "_same_volume", lambda a, b: True)
    cfg, inbox, library = _setup(tmp_path)
    dest_dir = library / "Boulder, Colorado, United States" / "2024-07-04"
    dest_dir.mkdir(parents=True)
    dest = dest_dir / "2024-07-04_09-15-00_DJI_0001.JPG"
    dest.write_bytes(b"capture-bytes")
    sha = move_engine.sha256_file(dest)
    gone_src = inbox / "DJI_0001.JPG"  # never on disk = renamed away pre-crash
    conn = _index(cfg)
    db.init_index_schema(conn)
    conn.execute(
        "INSERT INTO moves(batch_id, source_path, dest_path, source_sha256, status, started_at) "
        "VALUES ('B', ?, ?, ?, 'pending', datetime('now'))",
        (str(gone_src), str(dest), sha),
    )
    conn.commit()
    conn.close()

    organize.run_organize(cfg, assume_yes=True, extractor_factory=_factory({}))
    conn = _index(cfg)
    try:
        row = conn.execute(
            "SELECT status, dest_sha256 FROM moves WHERE source_path=?", (str(gone_src),)
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "source_deleted"  # reconciled from the move log
    assert row[1] == sha  # dest hash recorded (bytes identical to the renamed source)


def test_same_volume_rehashes_present_source_ignoring_stale_row(tmp_path, monkeypatch):
    # A prior crashed run left a 'pending' row with a WRONG hash for an inbox path, and
    # the file now present there has different bytes. The re-run must hash the CURRENT
    # bytes and persist files.sha256 = the real hash (not the stale row's), since the
    # source is present and re-hashable.
    monkeypatch.setattr(organize, "_same_volume", lambda a, b: True)
    cfg, inbox, library = _setup(tmp_path)
    src = _add(inbox, "DJI_0001.JPG", b"new-bytes")
    real_sha = move_engine.sha256_file(src)
    conn = _index(cfg)
    db.init_index_schema(conn)
    conn.execute(
        "INSERT INTO moves(batch_id, source_path, dest_path, source_sha256, status, started_at) "
        "VALUES ('OLD', ?, ?, ?, 'pending', datetime('now'))",
        (str(src), str(library / "old" / "x.JPG"), "00" * 32),  # stale, wrong hash
    )
    conn.commit()
    conn.close()

    report = organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0001.JPG": _md()})
    )
    assert report.organized == 1
    conn = _index(cfg)
    try:
        fsha = conn.execute("SELECT sha256 FROM files").fetchone()[0]
    finally:
        conn.close()
    assert fsha == real_sha  # fresh hash of the current bytes, not the stale "00…" row


def test_same_volume_failed_rename_resumes_not_duplicate(tmp_path, monkeypatch):
    # If a same-volume rename FAILS after the files row is persisted (e.g. EXDEV from a
    # wrong volume guess, or a locked dest), the source stays in the inbox. A re-run must
    # RE-ATTEMPT it — its own `failed` moves row makes it "mine", not a foreign duplicate
    # — and complete, rather than skip it as a duplicate and leave a phantom files row.
    monkeypatch.setattr(organize, "_same_volume", lambda a, b: True)
    cfg, inbox, library = _setup(tmp_path)
    src = _add(inbox, "DJI_0001.JPG", b"capture-bytes")
    real_rename = move_engine.rename_in_place
    state = {"fail": True}

    def _flaky(conn, batch, sp, dp, **k):
        if state["fail"]:  # mimic rename_in_place's failure bookkeeping (row->failed, no move)
            conn.execute(
                "UPDATE moves SET status='failed' WHERE source_path=? AND source_sha256=?",
                (str(sp), k.get("source_sha256")),
            )
            conn.commit()
            return move_engine.MoveOutcome("failed", k.get("source_sha256"), None, dp, "simulated EXDEV")
        return real_rename(conn, batch, sp, dp, **k)

    monkeypatch.setattr(organize.move_engine, "rename_in_place", _flaky)
    r1 = organize.run_organize(cfg, assume_yes=True, extractor_factory=_factory({"DJI_0001.JPG": _md()}))
    assert r1.aborted is True
    assert src.exists()  # source stranded in the inbox by the failed rename

    state["fail"] = False
    r2 = organize.run_organize(cfg, assume_yes=True, extractor_factory=_factory({"DJI_0001.JPG": _md()}))
    assert r2.organized == 1
    assert r2.duplicates_skipped == 0  # NOT skipped as a foreign duplicate
    assert not src.exists()
    conn = _index(cfg)
    try:
        nfiles = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        statuses = [s for (s,) in conn.execute(
            "SELECT status FROM moves WHERE source_path=?", (str(src),)
        ).fetchall()]
    finally:
        conn.close()
    assert nfiles == 1  # the phantom files row became real, not duplicated
    assert "source_deleted" in statuses  # the move completed on resume


def test_same_volume_foreign_duplicate_with_stale_row_is_skipped(tmp_path, monkeypatch):
    # A genuine duplicate (same content as an already-organized file, at a DIFFERENT inbox
    # path) must still be skipped even if that path carries a STALE move-log row from an
    # older, different-bytes attempt. The "mine" exemption is keyed on (source_path,
    # current hash), so a stale row with a different hash does not falsely exempt it.
    monkeypatch.setattr(organize, "_same_volume", lambda a, b: True)
    cfg, inbox, library = _setup(tmp_path)
    _add(inbox, "DJI_0001.JPG", b"dup-content")  # Q
    organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0001.JPG": _md()})
    )
    # Path P: a stale pending row (old, wrong hash), then the SAME content dropped at P.
    p = _add(inbox, "DJI_0002.JPG", b"dup-content")
    conn = _index(cfg)
    conn.execute(
        "INSERT INTO moves(batch_id, source_path, dest_path, source_sha256, status, started_at) "
        "VALUES ('OLD', ?, ?, ?, 'pending', datetime('now'))",
        (str(p), str(library / "old" / "p.JPG"), "11" * 32),
    )
    conn.commit()
    conn.close()

    # Pin relocation off here so the test stays focused on dedup DETECTION (the stale-row
    # "mine" exemption), not the relocation behavior covered by its own tests.
    report = organize.run_organize(
        replace(cfg, relocate_duplicates=False), assume_yes=True,
        extractor_factory=_factory({"DJI_0002.JPG": _md()}),
    )
    assert report.duplicates_skipped == 1  # P recognized as a foreign duplicate of Q
    assert report.organized == 0
    assert p.exists()  # dedup policy: skipped + left in the inbox
    conn = _index(cfg)
    try:
        nfiles = conn.execute(
            "SELECT COUNT(*) FROM files WHERE status='organized'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert nfiles == 1  # only Q filed — the duplicate was not filed a second time


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
