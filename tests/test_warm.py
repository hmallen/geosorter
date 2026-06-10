"""Tests for the post-organize warm pass (m-derived-at-scale).

``warm_library`` pre-generates thumbnails (photos) + posters (videos) for one
organized batch on the local cache tier, skips already-fresh assets (resumable),
and evicts the local tier down to ``cache_max_gb`` at the end. It reads the index
DB and calls the ``derived`` generators; it writes no library file.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from geosorter import db, derived, warm
from geosorter.config import Config

FIXTURES = Path(__file__).parent / "fixtures" / "media"


def _cfg(tmp_path: Path) -> Config:
    return Config(
        inbox_path=tmp_path / "inbox",
        library_root=tmp_path / "library",
        index_db_path=tmp_path / "index.db",
        geonames_db_path=tmp_path / "geonames.db",
        spatial_index="rtree",
        cache_dir=tmp_path / "cache",  # local tier off library_root
    )


def _seed_batch(cfg: Config, rows: list[tuple[str, str]], batch_id: str) -> None:
    conn = db.connect(cfg.index_db_path, integrity_check=False)
    db.init_index_schema(conn)
    for dest_path, media_type in rows:
        conn.execute(
            "INSERT INTO files(dest_path, filename, media_type, sha256, status, batch_id) "
            "VALUES (?,?,?,?,?,?)",
            (dest_path, Path(dest_path).name, media_type, "deadbeef", "organized", batch_id),
        )
    conn.commit()
    conn.close()


def test_warm_library_generates_thumbs_and_posters(tmp_path):
    cfg = _cfg(tmp_path)
    lib = cfg.library_root / "A"
    lib.mkdir(parents=True)
    photo = lib / "p.jpg"
    video = lib / "v.mp4"
    shutil.copy(FIXTURES / "dji_photo.jpg", photo)
    shutil.copy(FIXTURES / "h265_tiny.mp4", video)
    _seed_batch(cfg, [(str(photo), "photo"), (str(video), "video")], "b1")

    result = warm.warm_library(cfg, "b1")

    assert result.warmed == 2
    cache = cfg.cache_dir / derived.CACHE_DIRNAME
    thumbs = list((cache / "thumbs").rglob("*.jpg"))
    posters = list((cache / "posters").rglob("*.jpg"))
    assert len(thumbs) == 1  # the photo
    assert len(posters) == 1  # the video
    # The warm pass writes ONLY thumbs/posters — no previews, no proxies.
    assert not (cache / "previews").exists()
    assert not (cache / "proxies").exists()


def test_warm_library_skips_fresh(tmp_path):
    cfg = _cfg(tmp_path)
    lib = cfg.library_root / "A"
    lib.mkdir(parents=True)
    photo = lib / "p.jpg"
    shutil.copy(FIXTURES / "dji_photo.jpg", photo)
    _seed_batch(cfg, [(str(photo), "photo")], "b1")

    warm.warm_library(cfg, "b1")
    thumb = next((cfg.cache_dir / derived.CACHE_DIRNAME / "thumbs").rglob("*.jpg"))
    first = thumb.stat().st_mtime_ns

    warm.warm_library(cfg, "b1")  # resumable: a second pass regenerates nothing
    assert thumb.stat().st_mtime_ns == first


def test_warm_library_skips_missing_source(tmp_path):
    # A row whose library file is gone (moved by hand) is skipped, not fatal.
    cfg = _cfg(tmp_path)
    cfg.library_root.mkdir(parents=True)
    _seed_batch(cfg, [(str(cfg.library_root / "gone.jpg"), "photo")], "b1")
    result = warm.warm_library(cfg, "b1")
    assert result.warmed == 0
