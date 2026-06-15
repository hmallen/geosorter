"""Tests for collision recovery (re-file survivors of the recycled-filename bug).

The recycled-DJI-filename collision left library destinations holding the WRONG
bytes: a GPS-less 2023 ``staging`` clip overwrote the 2024 capture it collided with,
and the index ``files`` row still carries the lost 2024 capture's metadata.
``recover.run_recovery`` un-files each survivor to the inbox, drops the 3 wrong index
rows, and re-files the survivors through the fixed ``organize`` pipeline (GPS-less →
``_no-gps/<2023-date>/`` quarantine), recording the lost captures in a report.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from geosorter import db, geonames_loader, recover
from geosorter.config import Config
from geosorter.metadata import MediaMetadata

FIXTURES = Path(__file__).parent / "fixtures" / "geonames"

PREFIX = "\\\\?\\"  # the Windows long-path prefix organize stores on dest_path


def _md(
    *,
    media_type="video",
    lat=None,
    lon=None,
    gps_source="none",
    capture_ts_raw="2023:07:07 18:14:33",
    capture_ts_source_tag="QuickTime:CreateDate",
    width=3840,
    height=2160,
    duration_s=12.0,
    codec="h265",
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


def _index(cfg):
    return db.connect(cfg.index_db_path, integrity_check=False)


def _make_collision(
    cfg, library, *, orig="DJI_0123.MP4", survivor=b"survivor-2023-bytes"
):
    """Recreate one collision's on-disk + index state (the post-bug damage).

    A survivor file at the WRONG renamed 2024 destination, an index `files` row +
    bogus `file_companions` row + two `moves` rows (the 050725 primary and the
    staging companion) all pointing at that destination.
    """
    place = "Barrio San Luis, Bogota D.C., Colombia"
    dest_name = f"2024-03-17_16-12-34_{orig}"
    dest = library / place / "2024-03-17" / dest_name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(survivor)
    dest_str = PREFIX + str(dest)

    conn = _index(cfg)
    db.init_index_schema(conn)
    conn.execute(
        "INSERT INTO files(id, geonameid, place_string, dest_path, filename, media_type, "
        "capture_ts_utc, capture_ts_local, local_date, lat, lon, gps_source, codec, "
        "sha256, status, batch_id, created_at) VALUES "
        "(1, 9031040, ?, ?, ?, 'video', '2024-03-17T21:12:34+00:00', "
        "'2024-03-17T16:12:34-05:00', '2024-03-17', 4.6854, -74.0561, 'exif', 'h265', "
        "'cafef00d', 'organized', 'OLDBATCH', datetime('now'))",
        (place, dest_str, dest_name),
    )
    conn.execute(
        "INSERT INTO file_companions(primary_file_id, dest_path, companion_type) "
        "VALUES (1, ?, 'other')",
        (dest_str,),
    )
    conn.execute(
        "INSERT INTO moves(file_id, batch_id, source_path, dest_path, source_sha256, "
        "dest_sha256, status) VALUES (1, 'OLDBATCH', ?, ?, 'aaa', 'aaa', 'source_deleted')",
        (r"Z:\drones\ingest\050725\DCIM\102MEDIA\DJI_0123.MP4", dest_str),
    )
    conn.execute(
        "INSERT INTO moves(file_id, batch_id, source_path, dest_path, source_sha256, "
        "dest_sha256, status) VALUES (NULL, 'OLDBATCH', ?, ?, 'bbb', 'bbb', 'source_deleted')",
        (r"Z:\drones\ingest\staging\DJI_0123.MP4", dest_str),
    )
    conn.commit()
    conn.close()
    return dest, dest_str


def test_find_collisions(tmp_path):
    cfg, _inbox, library = _setup(tmp_path)
    dest, dest_str = _make_collision(cfg, library)
    conn = _index(cfg)
    try:
        cols = recover.find_collisions(conn)
    finally:
        conn.close()
    assert len(cols) == 1
    c = cols[0]
    assert c.primary_file_id == 1
    assert c.orig_name == "DJI_0123.MP4"  # from the staging (file_id NULL) moves row
    assert c.dest == dest_str
    assert c.local_date == "2024-03-17"  # the LOST 2024 capture's recorded date


def test_recovery_refiles_survivor_to_nogps(tmp_path):
    cfg, _inbox, library = _setup(tmp_path)
    _make_collision(cfg, library)

    report = recover.run_recovery(
        cfg, extractor_factory=_factory({"DJI_0123.MP4": _md()})
    )

    assert report.collisions_found == 1
    assert report.recovered == 1
    assert len(report.lost) == 1
    # Survivor re-filed to _no-gps by its REAL 2023 capture date.
    recovered = library / "_no-gps" / "2023-07-07" / "DJI_0123.MP4"
    assert recovered.exists()
    assert recovered.read_bytes() == b"survivor-2023-bytes"

    conn = _index(cfg)
    try:
        old = conn.execute("SELECT * FROM files WHERE id=1").fetchone()
        comp = conn.execute("SELECT COUNT(*) FROM file_companions").fetchone()[0]
        stale_moves = conn.execute(
            "SELECT COUNT(*) FROM moves WHERE dest_path LIKE '%2024-03-17_16-12-34_DJI_0123.MP4'"
        ).fetchone()[0]
        new = conn.execute(
            "SELECT status, gps_source, lat, lon, dest_path FROM files "
            "WHERE filename='DJI_0123.MP4'"
        ).fetchone()
    finally:
        conn.close()
    assert old is None  # phantom 2024 row gone
    assert comp == 0  # bogus companion row cascaded away
    assert stale_moves == 0  # the two old moves rows for the wrong dest are gone
    assert new is not None
    assert new[0] == "quarantined"
    assert new[1] == "none"
    assert new[2] is None and new[3] is None  # no GPS
    # The real 2023 capture date drives the quarantine folder (local_date stays NULL
    # for a no-GPS file; the date lives in the _no-gps/<date>/ path).
    assert os.path.join("_no-gps", "2023-07-07", "DJI_0123.MP4") in new[4]
    assert Path(report.report_path).exists()


def test_recovery_dry_run_changes_nothing(tmp_path):
    cfg, _inbox, library = _setup(tmp_path)
    dest, _dest_str = _make_collision(cfg, library)

    report = recover.run_recovery(
        cfg, dry_run=True, extractor_factory=_factory({"DJI_0123.MP4": _md()})
    )

    assert report.collisions_found == 1
    assert report.dry_run is True
    assert dest.exists()  # survivor not moved
    assert not (cfg.inbox_path / "_recovered_collisions").exists()  # staging dir not created
    assert report.report_path is None  # no report file written
    conn = _index(cfg)
    try:
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM moves").fetchone()[0] == 2
    finally:
        conn.close()


def test_recovery_cross_volume_relocate(tmp_path, monkeypatch):
    # Force _relocate's cross-volume fallback (copy + SHA-verify + delete) and confirm
    # the survivor is recovered intact (the data-loss-sensitive path).
    from geosorter import organize

    monkeypatch.setattr(organize, "_same_volume", lambda a, b: False)
    cfg, _inbox, library = _setup(tmp_path)
    _make_collision(cfg, library)

    report = recover.run_recovery(
        cfg, extractor_factory=_factory({"DJI_0123.MP4": _md()})
    )

    assert report.recovered == 1
    recovered = library / "_no-gps" / "2023-07-07" / "DJI_0123.MP4"
    assert recovered.exists()
    assert recovered.read_bytes() == b"survivor-2023-bytes"  # bytes intact, no loss


def test_relocate_cross_volume_failure_keeps_source(tmp_path, monkeypatch):
    # The data-safety guarantee: if the cross-volume copy fails, the partial dest is
    # cleaned up and the source (the sole surviving copy) is left intact. Without the
    # cleanup-on-OSError branch the partial would remain and block a re-run.
    from geosorter import move_engine

    src = tmp_path / "survivor.bin"
    src.write_bytes(b"irreplaceable")
    dst = tmp_path / "out" / "survivor.bin"

    def _boom(s, d, *a, **k):
        with open(d, "wb") as fh:
            fh.write(b"partial")  # a partial copy is left, as a real failure would
        raise OSError("simulated cross-volume copy failure")

    monkeypatch.setattr(move_engine, "_copy_file", _boom)
    with pytest.raises(OSError):
        recover._relocate(str(src), dst, same_volume=False)
    assert src.exists() and src.read_bytes() == b"irreplaceable"  # survivor intact
    assert not dst.exists()  # partial cleaned up — a re-run is not blocked


def test_recovery_on_uninitialized_index(tmp_path):
    # The verb must not crash when the index DB does not exist yet — report zero.
    cfg, _inbox, _library = _setup(tmp_path)  # index.db not created
    report = recover.run_recovery(cfg)
    assert report.collisions_found == 0
    assert report.recovered == 0


def test_recovery_no_collisions(tmp_path):
    cfg, _inbox, _library = _setup(tmp_path)
    conn = _index(cfg)
    db.init_index_schema(conn)
    conn.close()
    report = recover.run_recovery(cfg)
    assert report.collisions_found == 0
    assert report.recovered == 0
