"""Tests for the read-only inbox diagnostic (diagnose_inbox)."""

import hashlib
from pathlib import Path

from geosorter import db, diagnose
from geosorter.config import Config
from geosorter.metadata import MediaMetadata


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
    cfg = Config(
        inbox_path=inbox,
        library_root=library,
        index_db_path=tmp_path / "index.db",
        geonames_db_path=tmp_path / "geonames.db",  # never opened by diagnose
        spatial_index="rtree",
    )
    return cfg, inbox, library


def _add(inbox, name, data=b"capture-bytes"):
    p = inbox / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _by_name(diag, name):
    for d in diag.files:
        if d.path.name == name:
            return d
    raise AssertionError(f"{name} not found in diagnosis: {[d.path.name for d in diag.files]}")


def _seed_file(cfg, *, dest_path, sha256):
    conn = db.connect(cfg.index_db_path, integrity_check=False)
    db.init_index_schema(conn)
    conn.execute(
        "INSERT INTO files(dest_path, filename, media_type, sha256, status, batch_id) "
        "VALUES (?,?,?,?,?,?)",
        (dest_path, Path(dest_path).name, "photo", sha256, "organized", "seed-batch"),
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- #
def test_would_organize(tmp_path):
    cfg, inbox, _ = _setup(tmp_path)
    _add(inbox, "DJI_0001.JPG", b"real")
    diag = diagnose.diagnose_inbox(cfg, extractor_factory=_factory({"DJI_0001.JPG": _md()}))
    d = _by_name(diag, "DJI_0001.JPG")
    assert d.disposition == diagnose.WOULD_ORGANIZE


def test_no_gps_quarantine(tmp_path):
    cfg, inbox, _ = _setup(tmp_path)
    _add(inbox, "DJI_0002.JPG", b"nogps")
    md = _md(lat=None, lon=None, gps_source="none")
    diag = diagnose.diagnose_inbox(cfg, extractor_factory=_factory({"DJI_0002.JPG": md}))
    d = _by_name(diag, "DJI_0002.JPG")
    assert d.disposition == diagnose.WOULD_QUARANTINE
    assert d.reason == "no-gps"


def test_no_date_quarantine(tmp_path):
    cfg, inbox, _ = _setup(tmp_path)
    _add(inbox, "DJI_0007.JPG", b"nodate")
    md = _md(capture_ts_raw=None, capture_ts_source_tag=None)
    diag = diagnose.diagnose_inbox(cfg, extractor_factory=_factory({"DJI_0007.JPG": md}))
    d = _by_name(diag, "DJI_0007.JPG")
    assert d.disposition == diagnose.WOULD_QUARANTINE
    assert d.reason == "no-date"


def test_duplicate_detected(tmp_path):
    cfg, inbox, _ = _setup(tmp_path)
    data = b"dup-bytes"
    _add(inbox, "DJI_0003.JPG", data)
    sha = hashlib.sha256(data).hexdigest()
    existing = r"X:\library\Boulder\2024-07-04\2024-07-04_09-15-00_DJI_OLD.JPG"
    _seed_file(cfg, dest_path=existing, sha256=sha)

    diag = diagnose.diagnose_inbox(cfg, extractor_factory=_factory({"DJI_0003.JPG": _md()}))
    d = _by_name(diag, "DJI_0003.JPG")
    assert d.disposition == diagnose.DUPLICATE
    assert existing in (d.detail or "")


def test_no_gps_duplicate_reports_duplicate_not_quarantine(tmp_path):
    # A no-GPS capture whose content is already in the library is the SILENT duplicate
    # skip organize performs (dedup preempts the quarantine route), not a quarantine.
    cfg, inbox, _ = _setup(tmp_path)
    data = b"nogps-dup-bytes"
    _add(inbox, "DJI_0004.JPG", data)
    sha = hashlib.sha256(data).hexdigest()
    _seed_file(cfg, dest_path=r"X:\library\_no-gps\2024-01-01\DJI_OLD.JPG", sha256=sha)
    md = _md(lat=None, lon=None, gps_source="none")
    diag = diagnose.diagnose_inbox(cfg, extractor_factory=_factory({"DJI_0004.JPG": md}))
    assert _by_name(diag, "DJI_0004.JPG").disposition == diagnose.DUPLICATE


def test_unlinked_hyperlapse_frame_dir(tmp_path):
    # HYPERLAPSE frames with no matching flat render: prescan_inbox leaves them in
    # neither groups nor unclaimed, so only the leftover sweep accounts for them.
    cfg, inbox, _ = _setup(tmp_path)
    _add(inbox, "DCIM/HYPERLAPSE/001_0021/DJI_0001.JPG", b"frame1")
    _add(inbox, "DCIM/HYPERLAPSE/001_0021/DJI_0002.JPG", b"frame2")
    diag = diagnose.diagnose_inbox(cfg, extractor_factory=_factory({}))
    assert _by_name(diag, "DJI_0001.JPG").disposition == diagnose.UNLINKED_FRAME_DIR
    assert _by_name(diag, "DJI_0002.JPG").disposition == diagnose.UNLINKED_FRAME_DIR


def test_duplicate_not_flagged_when_hash_check_off(tmp_path):
    cfg, inbox, _ = _setup(tmp_path)
    data = b"dup-bytes"
    _add(inbox, "DJI_0003.JPG", data)
    sha = hashlib.sha256(data).hexdigest()
    _seed_file(cfg, dest_path=r"X:\library\x\DJI_OLD.JPG", sha256=sha)

    diag = diagnose.diagnose_inbox(
        cfg, hash_check=False, extractor_factory=_factory({"DJI_0003.JPG": _md()})
    )
    d = _by_name(diag, "DJI_0003.JPG")
    assert d.disposition == diagnose.WOULD_ORGANIZE


def test_non_dji_clutter(tmp_path):
    cfg, inbox, _ = _setup(tmp_path)
    _add(inbox, "random.jpg", b"x")
    diag = diagnose.diagnose_inbox(cfg, extractor_factory=_factory({}))
    d = _by_name(diag, "random.jpg")
    assert d.disposition == diagnose.NON_DJI_CLUTTER


def test_orphaned_sidecar(tmp_path):
    cfg, inbox, _ = _setup(tmp_path)
    _add(inbox, "DJI_0009.SRT", b"srt-with-no-primary")
    diag = diagnose.diagnose_inbox(cfg, extractor_factory=_factory({}))
    d = _by_name(diag, "DJI_0009.SRT")
    assert d.disposition == diagnose.ORPHANED_SIDECAR


def test_companions_share_primary_disposition(tmp_path):
    cfg, inbox, _ = _setup(tmp_path)
    _add(inbox, "DJI_0005.JPG", b"prim")
    _add(inbox, "DJI_0005.DNG", b"raw")
    diag = diagnose.diagnose_inbox(cfg, extractor_factory=_factory({"DJI_0005.JPG": _md()}))
    assert _by_name(diag, "DJI_0005.JPG").disposition == diagnose.WOULD_ORGANIZE
    dng = _by_name(diag, "DJI_0005.DNG")
    assert dng.disposition == diagnose.WOULD_ORGANIZE
    assert "companion" in dng.reason


def test_every_file_accounted_for_and_counts(tmp_path):
    cfg, inbox, _ = _setup(tmp_path)
    _add(inbox, "DJI_0001.JPG", b"a")
    _add(inbox, "random.jpg", b"b")
    _add(inbox, "DJI_0009.SRT", b"c")
    diag = diagnose.diagnose_inbox(cfg, extractor_factory=_factory({"DJI_0001.JPG": _md()}))
    assert len(diag.files) == 3
    assert sum(diag.counts.values()) == 3


def test_empty_or_missing_inbox(tmp_path):
    cfg = Config(
        inbox_path=tmp_path / "does-not-exist",
        library_root=tmp_path / "library",
        index_db_path=tmp_path / "index.db",
        geonames_db_path=tmp_path / "geonames.db",
        spatial_index="rtree",
    )
    diag = diagnose.diagnose_inbox(cfg)
    assert diag.files == []
    assert diag.counts == {}
