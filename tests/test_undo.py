"""Tests for the undo pipeline (reverse the most recent organize batch)."""

from __future__ import annotations

from pathlib import Path

from geosorter import db, geonames_loader, organize, undo
from geosorter.config import Config
from geosorter.metadata import MediaMetadata

FIXTURES = Path(__file__).parent / "fixtures" / "geonames"


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


def _counts(cfg, batch_id):
    conn = _index(cfg)
    try:
        m = conn.execute("SELECT COUNT(*) FROM moves WHERE batch_id=?", (batch_id,)).fetchone()[0]
        f = conn.execute("SELECT COUNT(*) FROM files WHERE batch_id=?", (batch_id,)).fetchone()[0]
        c = conn.execute("SELECT COUNT(*) FROM codec_stats WHERE batch_id=?", (batch_id,)).fetchone()[0]
    finally:
        conn.close()
    return m, f, c


# --------------------------------------------------------------------------- #
def test_undo_nothing_when_log_empty(tmp_path):
    cfg, _inbox, _library = _setup(tmp_path)
    report = undo.run_undo(cfg)
    assert report.nothing_to_undo is True
    assert report.batch_id is None
    assert report.restored == 0


def test_undo_round_trips_batch(tmp_path):
    cfg, inbox, library = _setup(tmp_path)
    src = _add(inbox, "DJI_0001.JPG", b"capture-bytes")
    organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0001.JPG": _md()})
    )
    assert not src.exists()  # organized away

    report = undo.run_undo(cfg)
    assert report.restored == 1
    assert report.conflicts == []
    assert report.failures == []
    assert src.exists()  # restored to original inbox path
    assert src.read_bytes() == b"capture-bytes"
    assert not any(p.is_file() for p in library.rglob("*"))  # library copy gone
    assert _counts(cfg, report.batch_id) == (0, 0, 0)  # batch index rows removed


def test_undo_restores_companions(tmp_path):
    cfg, inbox, library = _setup(tmp_path)
    prim = _add(inbox, "DJI_0001.MP4", b"video-bytes")
    comp = _add(inbox, "DJI_0001.SRT", b"srt-bytes")
    organize.run_organize(
        cfg,
        assume_yes=True,
        extractor_factory=_factory({"DJI_0001.MP4": _md(media_type="video", codec="h264")}),
    )
    assert not prim.exists() and not comp.exists()

    report = undo.run_undo(cfg)
    assert report.restored == 2
    assert prim.exists() and prim.read_bytes() == b"video-bytes"
    assert comp.exists() and comp.read_bytes() == b"srt-bytes"
    assert not any(p.is_file() for p in library.rglob("*"))
    assert _counts(cfg, report.batch_id) == (0, 0, 0)


def _make_catalog(path, rows):
    """Build a minimal DJI-shaped MISC catalog DB at ``path``; ``rows`` = (file_name, star)."""
    import sqlite3
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE gis_info_table (file_name TEXT, star INT)")
    conn.executemany("INSERT INTO gis_info_table(file_name, star) VALUES (?,?)", rows)
    conn.commit()
    conn.close()
    return path


def test_undo_restores_archived_catalog(tmp_path):
    # B11 archives MISC/*.db outside library_root via the crash-safe engine; undo
    # must reverse that archive (it is a logged moves row with file_id NULL).
    cfg, inbox, library = _setup(tmp_path)
    _add(inbox, "DCIM/DJI_001/DJI_20240825165234_0001_D.MP4", b"video-bytes")
    catalog = _make_catalog(
        inbox / "MISC" / "FC.db",
        [("/mnt/media_rw/sdcard0/DCIM/DJI_001/DJI_20240825165234_0001_D.MP4", 4)],
    )
    report = organize.run_organize(
        cfg,
        assume_yes=True,
        extractor_factory=_factory(
            {"DJI_20240825165234_0001_D.MP4": _md(media_type="video", codec="h264")}
        ),
    )
    archive = Path(cfg.index_db_path).parent / "catalogs" / report.batch_id / "FC.db"
    assert archive.is_file() and not catalog.exists()  # archived away

    ureport = undo.run_undo(cfg)
    assert ureport.conflicts == [] and ureport.failures == []
    assert catalog.is_file()  # the .db is restored to its inbox MISC path
    assert not archive.exists()  # the archive copy is removed
    assert _counts(cfg, report.batch_id) == (0, 0, 0)  # batch index rows removed


def test_undo_restores_panorama_unit(tmp_path):
    # A panorama capture is one primary + N panorama_frame companions filed under a
    # <stem>_frames/ subfolder; undo must reverse the whole unit (B12).
    cfg, inbox, library = _setup(tmp_path)
    tiles = [
        _add(inbox, f"DCIM/PANORAMA/001_0002/PANO_{i:04d}.JPG", f"tile-{i}".encode())
        for i in range(1, 6)
    ]
    mapping = {f"PANO_{i:04d}.JPG": _md() for i in range(1, 6)}
    organize.run_organize(cfg, assume_yes=True, extractor_factory=_factory(mapping))
    assert not any(t.exists() for t in tiles)  # whole unit organized away

    report = undo.run_undo(cfg)
    assert report.restored == 5  # primary + 4 panorama_frame companions
    assert all(t.exists() for t in tiles)
    assert tiles[0].read_bytes() == b"tile-1"
    assert not any(p.is_file() for p in library.rglob("*"))  # library copies gone
    assert _counts(cfg, report.batch_id) == (0, 0, 0)  # batch index rows removed


def test_undo_targets_only_the_latest_batch(tmp_path):
    cfg, inbox, _library = _setup(tmp_path)
    _add(inbox, "DJI_0001.JPG", b"first-content")
    organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0001.JPG": _md()})
    )
    _add(inbox, "DJI_0002.JPG", b"second-content")
    organize.run_organize(
        cfg,
        assume_yes=True,
        extractor_factory=_factory({"DJI_0002.JPG": _md(capture_ts_raw="2024:07:05 09:15:00")}),
    )

    conn = _index(cfg)
    try:
        latest = undo.latest_batch_id(conn)
        batch_of_2 = conn.execute(
            "SELECT batch_id FROM moves WHERE source_path LIKE '%DJI_0002.JPG'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert latest == batch_of_2

    report = undo.run_undo(cfg)
    assert report.batch_id == batch_of_2
    assert report.restored == 1
    assert (inbox / "DJI_0002.JPG").exists()  # latest batch reversed
    assert not (inbox / "DJI_0001.JPG").exists()  # earlier batch untouched


def test_undo_skips_conflicting_source(tmp_path):
    cfg, inbox, library = _setup(tmp_path)
    src = _add(inbox, "DJI_0001.JPG", b"original")
    organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0001.JPG": _md()})
    )
    assert not src.exists()
    src.write_bytes(b"a-totally-different-new-file")  # user dropped a new file here

    report = undo.run_undo(cfg)
    assert report.restored == 0
    assert report.conflicts == [str(src)]
    assert src.read_bytes() == b"a-totally-different-new-file"  # never clobbered
    assert any(p.is_file() for p in library.rglob("*"))  # library copy left in place
    m, f, _c = _counts(cfg, report.batch_id)
    assert m == 1 and f == 1  # rows kept (re-runnable)


def test_undo_resumes_after_partial_restore(tmp_path):
    cfg, inbox, library = _setup(tmp_path)
    src = _add(inbox, "DJI_0001.JPG", b"capture-bytes")
    organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0001.JPG": _md()})
    )
    conn = _index(cfg)
    try:
        dest_path = conn.execute("SELECT dest_path FROM moves").fetchone()[0]
    finally:
        conn.close()
    dest = Path(organize._strip(dest_path))
    # Simulate a crash mid-undo: source already restored, library copy + rows still present.
    src.write_bytes(b"capture-bytes")
    assert dest.exists()

    report = undo.run_undo(cfg)
    assert report.restored == 1
    assert src.exists() and src.read_bytes() == b"capture-bytes"
    assert not dest.exists()  # leftover library copy cleaned up on resume
    assert _counts(cfg, report.batch_id) == (0, 0, 0)


def test_undo_reports_missing_when_dest_gone(tmp_path):
    cfg, inbox, _library = _setup(tmp_path)
    src = _add(inbox, "DJI_0001.JPG")
    organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0001.JPG": _md()})
    )
    conn = _index(cfg)
    try:
        dest_path = conn.execute("SELECT dest_path FROM moves").fetchone()[0]
    finally:
        conn.close()
    Path(organize._strip(dest_path)).unlink()  # library copy lost externally

    report = undo.run_undo(cfg)
    assert report.missing == 1
    assert report.restored == 0
    assert not src.exists()


def test_undo_cancel_stops_and_keeps_remaining(tmp_path):
    cfg, inbox, _library = _setup(tmp_path)
    _add(inbox, "DJI_0001.JPG", b"one-content")
    _add(inbox, "DJI_0002.JPG", b"two-content")
    organize.run_organize(
        cfg,
        assume_yes=True,
        extractor_factory=_factory(
            {"DJI_0001.JPG": _md(), "DJI_0002.JPG": _md(capture_ts_raw="2024:07:05 09:15:00")}
        ),
    )
    # Cancel before any file is reversed.
    report = undo.run_undo(cfg, cancel=lambda: True)
    assert report.cancelled is True
    assert report.restored == 0
    m, f, _c = _counts(cfg, report.batch_id)
    assert m == 2 and f == 2  # nothing dropped — fully re-runnable


def test_undo_hyperlapse_300_companions(tmp_path):
    # A 300-frame hyperlapse render moves group-atomically and undo reverses every
    # source (render + 300 frames) back to the inbox, dropping all batch rows. This
    # exercises undo at scale without any undo-specific code change.
    import os

    cfg, inbox, library = _setup(tmp_path)
    render = _add(
        inbox, "DCIM/DJI_001/DJI_20240829183426_0021_D.MP4", b"render-bytes"
    )
    frames = []
    for i in range(1, 301):
        f = _add(
            inbox,
            f"DCIM/HYPERLAPSE/001_0021/HYPERLAPSE_{i:04d}.JPG",
            f"frame-{i}".encode(),
        )
        os.utime(f, (1000.0 + i, 1000.0 + i))
        frames.append(f)
    os.utime(render, (1400.0, 1400.0))
    mapping = {render.name: _md(media_type="video", lat=None, lon=None,
                                gps_source="none", codec="h264")}
    mapping.update({f"HYPERLAPSE_{i:04d}.JPG": _md() for i in range(1, 301)})

    org = organize.run_organize(cfg, assume_yes=True, extractor_factory=_factory(mapping))
    assert org.organized == 1 and org.companions == 300
    assert not render.exists() and not any(f.exists() for f in frames)

    report = undo.run_undo(cfg)
    assert report.restored == 301  # render + 300 frames
    assert report.conflicts == [] and report.failures == []
    assert render.exists() and all(f.exists() for f in frames)
    assert not any(p.is_file() for p in library.rglob("*"))  # library copies gone
    assert _counts(cfg, report.batch_id) == (0, 0, 0)
