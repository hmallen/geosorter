"""Tests for manual map-click re-tag (B8): re-file an organized capture to new GPS."""

from __future__ import annotations

import os
from pathlib import Path

from geosorter import db, geonames_loader, organize, retag
from geosorter.config import Config
from geosorter.metadata import MediaMetadata

FIXTURES = Path(__file__).parent / "fixtures" / "geonames"

# Two fixture cities in the SAME timezone (America/Denver) so a re-tag changes the
# place folder without shifting the date.
BOULDER = (40.015, -105.27)
DENVER = (39.73915, -104.9847)


def _md(*, lat=BOULDER[0], lon=BOULDER[1]):
    return MediaMetadata(
        "photo", lat, lon, "exif", "2024:07:04 09:15:00", "EXIF:DateTimeOriginal",
        4000, 3000, None, None, None, None, None,
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


def _add(inbox, name, data):
    p = inbox / name
    p.write_bytes(data)
    return p


def _index(cfg):
    return db.connect(cfg.index_db_path, integrity_check=False)


def _organize_one(cfg, inbox, *, with_companion=False):
    """Organize one Boulder photo (optionally + a .DNG companion); return its file id."""
    _add(inbox, "DJI_0001.JPG", b"primary-bytes")
    if with_companion:
        _add(inbox, "DJI_0001.DNG", b"raw-companion-bytes")
    organize.run_organize(
        cfg, assume_yes=True,
        extractor_factory=lambda: _FakeExtractor({"DJI_0001.JPG": _md()}),
    )
    conn = _index(cfg)
    try:
        return conn.execute("SELECT id FROM files").fetchone()[0]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
def test_retag_moves_organized_file_to_new_place(tmp_path):
    cfg, inbox, library = _setup(tmp_path)
    fid = _organize_one(cfg, inbox, with_companion=True)

    conn = _index(cfg)
    old_primary, old_gps = conn.execute(
        "SELECT dest_path, gps_source FROM files WHERE id=?", (fid,)
    ).fetchone()
    old_companion = conn.execute(
        "SELECT dest_path FROM file_companions WHERE primary_file_id=?", (fid,)
    ).fetchone()[0]
    old_sha = conn.execute(
        "SELECT dest_sha256 FROM moves WHERE file_id=?", (fid,)
    ).fetchone()[0]
    conn.close()
    assert "Boulder" in old_primary
    assert old_gps == "exif"

    report = retag.retag_file(cfg, fid, *DENVER)
    assert report.status == "retagged"
    assert report.moved == 2  # primary + companion physically moved

    conn = _index(cfg)
    new_primary, place, lat, lon, gps = conn.execute(
        "SELECT dest_path, place_string, lat, lon, gps_source FROM files WHERE id=?", (fid,)
    ).fetchone()
    new_companion = conn.execute(
        "SELECT dest_path FROM file_companions WHERE primary_file_id=?", (fid,)
    ).fetchone()[0]
    mv_dest, mv_sha = conn.execute(
        "SELECT dest_path, dest_sha256 FROM moves WHERE file_id=?", (fid,)
    ).fetchone()
    conn.close()

    assert place == "Denver, Colorado, United States"
    assert "Denver" in new_primary and new_primary.endswith("DJI_0001.JPG")
    assert (lat, lon) == DENVER
    assert gps == "manual"
    assert "Denver" in new_companion and new_companion.endswith(".DNG")
    # disk: new exists, old gone
    assert os.path.exists(organize._strip(new_primary))
    assert os.path.exists(organize._strip(new_companion))
    assert not os.path.exists(organize._strip(old_primary))
    assert not os.path.exists(organize._strip(old_companion))
    # moves row points at the new dest; content hash unchanged
    assert "Denver" in mv_dest
    assert mv_sha == old_sha


def test_retag_invalidates_derived_cache(tmp_path, monkeypatch):
    # (m-fix-stale-derived-cache-thumbnails) A re-tag rewrites content at the OLD and NEW
    # dest paths, so the derived cache for both (primary + companion) must be invalidated
    # — otherwise a stale poster/thumbnail/proxy from the prior content keeps being served.
    cfg, inbox, _ = _setup(tmp_path)
    fid = _organize_one(cfg, inbox, with_companion=True)

    calls = []
    monkeypatch.setattr(
        retag.derived, "invalidate",
        lambda cache_dir, proxy_cache_dir, rel_key: calls.append(rel_key),
    )
    report = retag.retag_file(cfg, fid, *DENVER)
    assert report.status == "retagged"

    assert any("Boulder" in r and r.endswith("DJI_0001.JPG") for r in calls)  # old primary
    assert any("Denver" in r and r.endswith("DJI_0001.JPG") for r in calls)   # new primary
    assert any("Boulder" in r and r.endswith(".DNG") for r in calls)          # old companion
    assert any("Denver" in r and r.endswith(".DNG") for r in calls)           # new companion


def test_retag_is_idempotent_when_repeated(tmp_path):
    # Re-tagging twice to the same new place is a no-op the second time: the file
    # is already at the Denver path, so nothing moves and no data is lost.
    cfg, inbox, _ = _setup(tmp_path)
    fid = _organize_one(cfg, inbox)
    first = retag.retag_file(cfg, fid, *DENVER)
    assert first.status == "retagged" and first.moved == 1

    second = retag.retag_file(cfg, fid, *DENVER)
    assert second.status == "retagged"
    assert second.moved == 0  # already at the Denver path

    conn = _index(cfg)
    dest = conn.execute("SELECT dest_path FROM files WHERE id=?", (fid,)).fetchone()[0]
    conn.close()
    assert "Denver" in dest
    assert os.path.exists(organize._strip(dest))


def _organize_at(cfg, inbox, subdir, content, lat, lon):
    # Distinct inbox subdir per run -> distinct source_path (so the second run is
    # not skipped by is_already_moved), but the same DJI_0001 stem -> the same
    # destination filename, which is what forces a re-tag collision.
    d = inbox / subdir
    d.mkdir(parents=True, exist_ok=True)
    (d / "DJI_0001.JPG").write_bytes(content)
    organize.run_organize(
        cfg, assume_yes=True,
        extractor_factory=lambda: _FakeExtractor({"DJI_0001.JPG": _md(lat=lat, lon=lon)}),
    )


def test_retag_collision_with_different_file_gets_suffix(tmp_path):
    # File B is already filed at Denver. Re-tagging file A (Boulder) to Denver
    # computes the SAME path B occupies -> must be suffixed (files.dest_path is
    # UNIQUE), not raise an IntegrityError or orphan a copy.
    cfg, inbox, _ = _setup(tmp_path)
    _organize_at(cfg, inbox, "r1", b"content-B", *DENVER)   # -> Denver/.../..._DJI_0001.JPG
    _organize_at(cfg, inbox, "r2", b"content-A", *BOULDER)  # -> Boulder/.../..._DJI_0001.JPG (same name)

    conn = _index(cfg)
    a_id = conn.execute(
        "SELECT id FROM files WHERE dest_path LIKE '%Boulder%'"
    ).fetchone()[0]
    conn.close()

    report = retag.retag_file(cfg, a_id, *DENVER)
    assert report.status == "retagged"

    conn = _index(cfg)
    a_dest = conn.execute("SELECT dest_path, filename FROM files WHERE id=?", (a_id,)).fetchone()
    denver_count = conn.execute(
        "SELECT COUNT(*) FROM files WHERE dest_path LIKE '%Denver%'"
    ).fetchone()[0]
    conn.close()

    assert "Denver" in a_dest[0] and a_dest[0].endswith("_2.JPG")  # disambiguated
    assert a_dest[1].endswith("_2.JPG")  # filename column tracks the suffixed basename
    assert denver_count == 2  # both files coexist at Denver
    assert os.path.exists(organize._strip(a_dest[0]))


def test_retag_unknown_id_is_not_found(tmp_path):
    cfg, inbox, _ = _setup(tmp_path)
    _organize_one(cfg, inbox)
    report = retag.retag_file(cfg, 9999, *DENVER)
    assert report.status == "not_found"
    assert report.moved == 0


def test_retag_same_spot_updates_provenance_without_moving(tmp_path):
    cfg, inbox, _ = _setup(tmp_path)
    fid = _organize_one(cfg, inbox)
    conn = _index(cfg)
    old_primary = conn.execute("SELECT dest_path FROM files WHERE id=?", (fid,)).fetchone()[0]
    conn.close()

    # Re-tag to (essentially) the same Boulder coordinate -> same dest, no move.
    report = retag.retag_file(cfg, fid, *BOULDER)
    assert report.status == "retagged"
    assert report.moved == 0

    conn = _index(cfg)
    new_primary, gps = conn.execute(
        "SELECT dest_path, gps_source FROM files WHERE id=?", (fid,)
    ).fetchone()
    conn.close()
    assert new_primary == old_primary  # unchanged path
    assert gps == "manual"  # provenance still recorded
    assert os.path.exists(organize._strip(new_primary))
