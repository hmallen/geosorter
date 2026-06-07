"""Tests for the rescan pipeline (reconcile the index with on-disk state).

``rescan`` prunes ``files`` rows whose library ``dest_path`` no longer exists on
disk (e.g. a capture moved out of ``library_root`` by hand). It mutates the index
DB ONLY — it never deletes or moves a file on disk.
"""

from __future__ import annotations

from pathlib import Path

from geosorter import db, geonames_loader, organize, rescan
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


def _primary(cfg):
    """The single (id, on-disk dest path) of the only organized/quarantined file."""
    conn = _index(cfg)
    try:
        fid, dest = conn.execute("SELECT id, dest_path FROM files").fetchone()
    finally:
        conn.close()
    return fid, Path(organize._strip(dest))


def _companion_paths(cfg, primary_id):
    conn = _index(cfg)
    try:
        rows = conn.execute(
            "SELECT dest_path FROM file_companions WHERE primary_file_id=?", (primary_id,)
        ).fetchall()
    finally:
        conn.close()
    return [Path(organize._strip(r[0])) for r in rows]


def _total_rows(cfg):
    conn = _index(cfg)
    try:
        m = conn.execute("SELECT COUNT(*) FROM moves").fetchone()[0]
        f = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    finally:
        conn.close()
    return m, f


# --------------------------------------------------------------------------- #
def test_rescan_nothing_when_index_empty(tmp_path):
    cfg, _inbox, _library = _setup(tmp_path)
    report = rescan.run_rescan(cfg)
    assert report.checked == 0
    assert report.pruned == 0
    assert report.kept == 0
    assert report.warnings == []
    assert report.orphaned == []


def test_keeps_present_files(tmp_path):
    cfg, inbox, _library = _setup(tmp_path)
    _add(inbox, "DJI_0001.JPG")
    report = organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0001.JPG": _md()})
    )
    before = _total_rows(cfg)

    rep = rescan.run_rescan(cfg)
    assert rep.checked == 1
    assert rep.kept == 1
    assert rep.pruned == 0
    assert _total_rows(cfg) == before  # nothing removed


def test_prunes_missing_primary(tmp_path):
    cfg, inbox, _library = _setup(tmp_path)
    _add(inbox, "DJI_0001.JPG")
    report = organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0001.JPG": _md()})
    )
    _fid, dest = _primary(cfg)
    dest.unlink()  # capture moved out of the library by hand

    rep = rescan.run_rescan(cfg)
    assert rep.checked == 1
    assert rep.pruned == 1
    assert rep.kept == 0
    assert _counts(cfg, report.batch_id) == (0, 0, 0)  # files + moves + codec rows gone


def test_prunes_missing_quarantined(tmp_path):
    cfg, inbox, _library = _setup(tmp_path)
    _add(inbox, "DJI_0001.JPG")
    report = organize.run_organize(
        cfg,
        assume_yes=True,
        extractor_factory=_factory(
            {"DJI_0001.JPG": _md(lat=None, lon=None, gps_source="none")}
        ),
    )
    assert report.quarantined == 1
    _fid, dest = _primary(cfg)
    assert "_no-gps" in str(dest)
    dest.unlink()

    rep = rescan.run_rescan(cfg)
    assert rep.pruned == 1
    assert _total_rows(cfg) == (0, 0)


def test_partial_companion_warns_without_pruning(tmp_path):
    cfg, inbox, _library = _setup(tmp_path)
    _add(inbox, "DJI_0001.MP4", b"video-bytes")
    _add(inbox, "DJI_0001.SRT", b"srt-bytes")
    organize.run_organize(
        cfg,
        assume_yes=True,
        extractor_factory=_factory({"DJI_0001.MP4": _md(media_type="video", codec="h264")}),
    )
    fid, primary = _primary(cfg)
    comp = _companion_paths(cfg, fid)[0]
    comp.unlink()  # only the companion left the library; primary is still present

    rep = rescan.run_rescan(cfg)
    assert rep.kept == 1
    assert rep.pruned == 0
    assert len(rep.warnings) == 1
    assert "DJI_0001" in rep.warnings[0]
    assert primary.exists()  # primary untouched on disk
    assert _total_rows(cfg) == (2, 1)  # rows intact (1 primary move + 1 companion move)


def test_orphaned_companion_reported_not_deleted(tmp_path):
    cfg, inbox, _library = _setup(tmp_path)
    _add(inbox, "DJI_0001.MP4", b"video-bytes")
    _add(inbox, "DJI_0001.SRT", b"srt-bytes")
    organize.run_organize(
        cfg,
        assume_yes=True,
        extractor_factory=_factory({"DJI_0001.MP4": _md(media_type="video", codec="h264")}),
    )
    fid, primary = _primary(cfg)
    comp = _companion_paths(cfg, fid)[0]
    primary.unlink()  # primary moved out; companion left stranded on disk

    rep = rescan.run_rescan(cfg)
    assert rep.pruned == 1
    assert rep.orphaned == [str(comp)]
    assert comp.exists()  # rescan never deletes a file on disk
    assert _total_rows(cfg) == (0, 0)  # whole group's rows pruned


def test_codec_stats_dropped_when_batch_empties(tmp_path):
    cfg, inbox, _library = _setup(tmp_path)
    _add(inbox, "DJI_0001.MP4", b"video-bytes")
    report = organize.run_organize(
        cfg,
        assume_yes=True,
        extractor_factory=_factory({"DJI_0001.MP4": _md(media_type="video", codec="h264")}),
    )
    assert _counts(cfg, report.batch_id)[2] == 1  # codec_stats row present
    _fid, dest = _primary(cfg)
    dest.unlink()

    rescan.run_rescan(cfg)
    assert _counts(cfg, report.batch_id) == (0, 0, 0)  # codec_stats dropped too


def test_dry_run_writes_nothing(tmp_path):
    cfg, inbox, _library = _setup(tmp_path)
    _add(inbox, "DJI_0001.MP4", b"video-bytes")
    report = organize.run_organize(
        cfg,
        assume_yes=True,
        extractor_factory=_factory({"DJI_0001.MP4": _md(media_type="video", codec="h264")}),
    )
    _fid, dest = _primary(cfg)
    dest.unlink()
    before = _counts(cfg, report.batch_id)

    rep = rescan.run_rescan(cfg, dry_run=True)
    assert rep.dry_run is True
    assert rep.pruned == 1  # reports what WOULD be pruned
    assert _counts(cfg, report.batch_id) == before  # but nothing written


def test_disk_untouched_for_surviving_group(tmp_path):
    cfg, inbox, _library = _setup(tmp_path)
    _add(inbox, "DJI_0001.JPG", b"first-content")
    _add(inbox, "DJI_0002.JPG", b"second-content")
    organize.run_organize(
        cfg,
        assume_yes=True,
        extractor_factory=_factory(
            {"DJI_0001.JPG": _md(), "DJI_0002.JPG": _md(capture_ts_raw="2024:07:05 09:15:00")}
        ),
    )
    conn = _index(cfg)
    try:
        rows = conn.execute("SELECT id, dest_path FROM files ORDER BY id").fetchall()
    finally:
        conn.close()
    gone = Path(organize._strip(rows[0][1]))
    kept = Path(organize._strip(rows[1][1]))
    gone.unlink()  # first capture moved out of the library

    rep = rescan.run_rescan(cfg)
    assert rep.checked == 2
    assert rep.pruned == 1
    assert rep.kept == 1
    assert kept.exists() and kept.read_bytes() == b"second-content"  # survivor untouched
