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


def _seed_batch(cfg: Config, rows: list[tuple], batch_id: str) -> None:
    # Each row is (dest_path, media_type) or (dest_path, media_type, codec).
    conn = db.connect(cfg.index_db_path, integrity_check=False)
    db.init_index_schema(conn)
    for row in rows:
        dest_path, media_type = row[0], row[1]
        codec = row[2] if len(row) > 2 else None
        conn.execute(
            "INSERT INTO files(dest_path, filename, media_type, codec, sha256, status, batch_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (dest_path, Path(dest_path).name, media_type, codec, "deadbeef", "organized", batch_id),
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


# --- Proxy pre-warm + proxy-tier cap (m-implement-proxy-prewarm-cap) -------- #


def _proxy_cfg(tmp_path: Path, **over) -> Config:
    return Config(
        inbox_path=tmp_path / "inbox",
        library_root=tmp_path / "library",
        index_db_path=tmp_path / "index.db",
        geonames_db_path=tmp_path / "geonames.db",
        spatial_index="rtree",
        cache_dir=tmp_path / "cache",
        proxy_cache_dir=tmp_path / "proxytier",  # explicit, off the library
        **over,
    )


def test_warm_library_warms_proxies_when_opted_in(tmp_path, monkeypatch):
    cfg = _proxy_cfg(tmp_path, warm_proxies=True)
    lib = cfg.library_root / "A"
    lib.mkdir(parents=True)
    video = lib / "v.mp4"
    shutil.copy(FIXTURES / "h265_tiny.mp4", video)
    _seed_batch(cfg, [(str(video), "video", "h265")], "b1")

    calls: list[tuple] = []

    def fake_proxy(cache_root, rel_key, source, codec):
        calls.append((Path(cache_root), rel_key, Path(source), codec))
        out = derived._cache_path(cache_root, rel_key, "proxies", ".mp4")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"proxy")
        return out  # != source → a real transcode

    monkeypatch.setattr(derived, "proxy", fake_proxy)
    result = warm.warm_library(cfg, "b1")

    assert len(calls) == 1
    assert calls[0][0] == cfg.proxy_cache_dir  # proxy tier, not the local cache_dir
    assert calls[0][3] == "h265"  # codec threaded from the DB
    assert result.proxies_warmed == 1
    proxies = list((cfg.proxy_cache_dir / derived.CACHE_DIRNAME / "proxies").rglob("*.mp4"))
    assert len(proxies) == 1


def test_warm_library_skips_proxies_by_default(tmp_path, monkeypatch):
    cfg = _proxy_cfg(tmp_path)  # warm_proxies defaults False
    lib = cfg.library_root / "A"
    lib.mkdir(parents=True)
    video = lib / "v.mp4"
    shutil.copy(FIXTURES / "h265_tiny.mp4", video)
    _seed_batch(cfg, [(str(video), "video", "h265")], "b1")

    called = False

    def fake_proxy(*a, **k):
        nonlocal called
        called = True

    monkeypatch.setattr(derived, "proxy", fake_proxy)
    result = warm.warm_library(cfg, "b1")

    assert called is False
    assert result.proxies_warmed == 0
    assert not (cfg.proxy_cache_dir / derived.CACHE_DIRNAME / "proxies").exists()


def test_warm_library_evicts_proxy_tier_when_capped(tmp_path):
    # The cap is enforced INDEPENDENT of warm_proxies (here it is off): a pre-seeded
    # proxy tier over the cap is swept down at the warm-pass boundary.
    cfg = _proxy_cfg(tmp_path, proxy_cache_max_gb=2 / 1024)  # 2 MiB cap
    cfg.library_root.mkdir(parents=True)
    proxies = cfg.proxy_cache_dir / derived.CACHE_DIRNAME / "proxies"
    proxies.mkdir(parents=True)
    mib = 1 << 20
    import os
    for i in range(4):
        f = proxies / f"p{i}.mp4"
        f.write_bytes(b"\0" * mib)
        os.utime(f, (1000 + i, 1000 + i))
    _seed_batch(cfg, [], "b1")  # empty batch; only the post-loop eviction matters

    result = warm.warm_library(cfg, "b1")

    assert result.proxy_eviction is not None
    assert result.proxy_eviction.bytes_after <= 2 * mib
    assert result.proxy_eviction.deleted >= 2


def test_warm_library_no_proxy_eviction_when_uncapped(tmp_path):
    cfg = _proxy_cfg(tmp_path)  # proxy_cache_max_gb defaults None
    cfg.library_root.mkdir(parents=True)
    _seed_batch(cfg, [], "b1")
    result = warm.warm_library(cfg, "b1")
    assert result.proxy_eviction is None
