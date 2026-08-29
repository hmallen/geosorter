"""Tests for the assign-location engine (promote no-GPS quarantine -> organized)."""

import os
from pathlib import Path

from geosorter import db, geonames_loader, organize, setloc
from geosorter.config import Config
from geosorter.metadata import MediaMetadata

FIXTURES = Path(__file__).parent / "fixtures" / "geonames"

BOULDER = (40.015, -105.27)  # geonameid 5574991 -> "Boulder, Colorado, United States"


def _md(
    *,
    media_type="photo",
    lat=None,
    lon=None,
    gps_source="none",
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


def _quarantine(cfg, inbox, names_to_md):
    """Run organize on no-GPS captures so they quarantine; return their file ids."""
    for name in names_to_md:
        _add(inbox, name, data=f"bytes-{name}".encode())  # distinct -> no hash-dedup
    organize.run_organize(cfg, assume_yes=True, extractor_factory=_factory(names_to_md))
    conn = _index(cfg)
    try:
        rows = conn.execute(
            "SELECT id, filename FROM files WHERE status='quarantined' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return {r[1]: r[0] for r in rows}


def test_assign_single_quarantined_promotes(tmp_path):
    cfg, inbox, library = _setup(tmp_path)
    ids = _quarantine(cfg, inbox, {"DJI_0001.JPG": _md()})
    fid = ids["DJI_0001.JPG"]
    conn = _index(cfg)
    try:
        conn.execute("UPDATE files SET no_gps_hidden=1 WHERE id=?", (fid,))
        conn.commit()
    finally:
        conn.close()

    report = setloc.assign_locations(
        cfg, [fid], *BOULDER, extractor_factory=_factory({"DJI_0001.JPG": _md()})
    )

    assert report.assigned == 1
    assert report.skipped == 0
    assert report.place_string == "Boulder, Colorado, United States"
    new = library / "Boulder, Colorado, United States" / "2024-07-04" / \
        "2024-07-04_09-15-00_DJI_0001.JPG"
    assert new.is_file()
    assert not (library / "_no-gps" / "2024-07-04" / "DJI_0001.JPG").exists()
    conn = _index(cfg)
    try:
        row = conn.execute(
            "SELECT status, gps_source, lat, lon, place_string, local_date, "
            "no_gps_hidden "
            "FROM files WHERE id=?", (fid,)
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "organized"
    assert row[1] == "manual"
    assert round(row[2], 3) == 40.015 and round(row[3], 3) == -105.27
    assert row[4] == "Boulder, Colorado, United States"
    assert row[5] == "2024-07-04"
    assert row[6] == 0


def test_assign_invalidates_derived_cache(tmp_path, monkeypatch):
    # (m-fix-stale-derived-cache-thumbnails) Promoting a no-GPS capture rewrites content
    # at a NEW library dest, so the derived cache for that path must be invalidated.
    cfg, inbox, library = _setup(tmp_path)
    ids = _quarantine(cfg, inbox, {"DJI_0001.JPG": _md()})
    fid = ids["DJI_0001.JPG"]

    calls = []
    monkeypatch.setattr(
        setloc.derived, "invalidate",
        lambda cache_dir, proxy_cache_dir, rel_key: calls.append(rel_key),
    )
    report = setloc.assign_locations(
        cfg, [fid], *BOULDER, extractor_factory=_factory({"DJI_0001.JPG": _md()})
    )
    assert report.assigned == 1
    assert any("Boulder" in r and r.endswith("DJI_0001.JPG") for r in calls)  # new dest


def test_assign_bulk_each_to_own_date(tmp_path):
    cfg, inbox, library = _setup(tmp_path)
    md1 = _md(capture_ts_raw="2024:07:04 09:15:00")
    md2 = _md(capture_ts_raw="2023:07:07 18:14:33")
    ids = _quarantine(cfg, inbox, {"DJI_0001.JPG": md1, "DJI_0002.JPG": md2})

    report = setloc.assign_locations(
        cfg, list(ids.values()), *BOULDER,
        extractor_factory=_factory({"DJI_0001.JPG": md1, "DJI_0002.JPG": md2}),
    )

    assert report.assigned == 2
    base = library / "Boulder, Colorado, United States"
    assert (base / "2024-07-04" / "2024-07-04_09-15-00_DJI_0001.JPG").is_file()
    assert (base / "2023-07-07" / "2023-07-07_18-14-33_DJI_0002.JPG").is_file()


def test_assign_moves_companions(tmp_path):
    cfg, inbox, library = _setup(tmp_path)
    # A no-GPS video with a sibling .SRT companion (grouped by organize).
    md = _md(media_type="video", codec="h264")
    _add(inbox, "DJI_0003.MP4")
    _add(inbox, "DJI_0003.SRT", data=b"srt-bytes")
    organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0003.MP4": md}),
    )
    conn = _index(cfg)
    fid = conn.execute(
        "SELECT id FROM files WHERE status='quarantined'"
    ).fetchone()[0]
    conn.close()

    report = setloc.assign_locations(
        cfg, [fid], *BOULDER, extractor_factory=_factory({"DJI_0003.MP4": md})
    )

    assert report.assigned == 1
    base = library / "Boulder, Colorado, United States" / "2024-07-04"
    assert (base / "2024-07-04_09-15-00_DJI_0003.MP4").is_file()
    assert (base / "2024-07-04_09-15-00_DJI_0003.SRT").is_file()
    conn = _index(cfg)
    try:
        comp = conn.execute(
            "SELECT dest_path FROM file_companions WHERE primary_file_id=?", (fid,)
        ).fetchone()
    finally:
        conn.close()
    assert comp[0].endswith("2024-07-04_09-15-00_DJI_0003.SRT")


def test_assign_progress_ticks_once_per_capture(tmp_path):
    # Regression: the progress callback must fire exactly ONCE per capture, even
    # when the capture has companions. The job denominator counts captures, so a
    # per-file tick would push processed past total ("6 of 4").
    cfg, inbox, library = _setup(tmp_path)
    md = _md(media_type="video", codec="h264")
    _add(inbox, "DJI_0003.MP4")
    _add(inbox, "DJI_0003.SRT", data=b"srt-bytes")  # companion -> 2 files in the group
    organize.run_organize(
        cfg, assume_yes=True, extractor_factory=_factory({"DJI_0003.MP4": md}),
    )
    conn = _index(cfg)
    fid = conn.execute(
        "SELECT id FROM files WHERE status='quarantined'"
    ).fetchone()[0]
    conn.close()

    calls: list[str] = []
    report = setloc.assign_locations(
        cfg, [fid], *BOULDER,
        progress=calls.append,
        extractor_factory=_factory({"DJI_0003.MP4": md}),
    )

    assert report.assigned == 1
    # One capture in -> exactly one tick out, regardless of the companion count.
    assert len(calls) == 1


def test_assign_undated_falls_back_to_mtime(tmp_path):
    cfg, inbox, library = _setup(tmp_path)
    md = _md(capture_ts_raw=None, capture_ts_source_tag=None)
    ids = _quarantine(cfg, inbox, {"DJI_0004.JPG": md})
    fid = ids["DJI_0004.JPG"]

    report = setloc.assign_locations(
        cfg, [fid], *BOULDER, extractor_factory=_factory({"DJI_0004.JPG": md})
    )

    assert report.assigned == 1
    conn = _index(cfg)
    try:
        row = conn.execute(
            "SELECT status, local_date FROM files WHERE id=?", (fid,)
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "organized"
    assert row[1] is not None  # date derived from the file mtime


def test_assign_idempotent_rerun(tmp_path):
    cfg, inbox, library = _setup(tmp_path)
    ids = _quarantine(cfg, inbox, {"DJI_0001.JPG": _md()})
    fid = ids["DJI_0001.JPG"]
    setloc.assign_locations(
        cfg, [fid], *BOULDER, extractor_factory=_factory({"DJI_0001.JPG": _md()})
    )
    # Re-running with the now-organized id is a no-op: nothing left to assign.
    report = setloc.assign_locations(
        cfg, [fid], *BOULDER, extractor_factory=_factory({"DJI_0001.JPG": _md()})
    )
    assert report.assigned == 0
    assert report.skipped == 1


def test_assign_skips_unknown_and_non_quarantined(tmp_path):
    cfg, inbox, library = _setup(tmp_path)
    # One quarantined + one organized capture.
    _add(inbox, "DJI_0005.JPG")
    organize.run_organize(
        cfg, assume_yes=True,
        extractor_factory=_factory({"DJI_0005.JPG": _md(lat=40.015, lon=-105.27,
                                                        gps_source="exif")}),
    )
    ids = _quarantine(cfg, inbox, {"DJI_0001.JPG": _md()})
    quarantined_fid = ids["DJI_0001.JPG"]
    conn = _index(cfg)
    organized_fid = conn.execute(
        "SELECT id FROM files WHERE status='organized' AND filename LIKE '%DJI_0005%'"
    ).fetchone()[0]
    conn.close()

    report = setloc.assign_locations(
        cfg, [99999, organized_fid, quarantined_fid], *BOULDER,
        extractor_factory=_factory({"DJI_0001.JPG": _md()}),
    )
    assert report.assigned == 1  # only the quarantined one
    assert report.skipped == 2   # unknown id + already-organized id
