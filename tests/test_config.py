"""Tests for config loading and the starter writer."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from geosorter import config


def test_load_missing_file_returns_defaults(tmp_path):
    cfg = config.load(tmp_path / "nope.toml")
    assert cfg.spatial_index == "rtree"
    assert cfg.index_db_path.name == "index.db"
    assert cfg.geonames_db_path.name == "geonames.db"
    assert cfg.inbox_path is None
    assert cfg.library_root is None


def test_write_starter_then_load_roundtrip(tmp_path):
    cfg_path = tmp_path / "geosorter.toml"
    written = config.write_starter(cfg_path)
    assert written == cfg_path
    assert cfg_path.exists()

    cfg = config.load(cfg_path)
    assert cfg.spatial_index == "rtree"
    assert cfg.index_db_path.name == "index.db"


def test_write_starter_refuses_overwrite(tmp_path):
    cfg_path = tmp_path / "geosorter.toml"
    config.write_starter(cfg_path)
    with pytest.raises(FileExistsError):
        config.write_starter(cfg_path)
    config.write_starter(cfg_path, overwrite=True)  # explicit overwrite is allowed


def test_env_var_resolution(tmp_path, monkeypatch):
    cfg_path = tmp_path / "fromenv.toml"
    cfg_path.write_text("spatial_index = 'columnar'\n", encoding="utf-8")
    monkeypatch.setenv("GEOSORTER_CONFIG", str(cfg_path))
    assert config.resolve_config_path() == cfg_path
    assert config.load().spatial_index == "columnar"


def test_feature_proximity_km_default(tmp_path):
    # No config file → the prefer-nearest-feature radius defaults to 5.0 km.
    assert config.load(tmp_path / "nope.toml").feature_proximity_km == 5.0


def test_feature_proximity_km_override(tmp_path):
    cfg_path = tmp_path / "geosorter.toml"
    cfg_path.write_text("feature_proximity_km = 3.0\n", encoding="utf-8")
    assert config.load(cfg_path).feature_proximity_km == 3.0


def test_inference_max_gap_minutes_default(tmp_path):
    # No config file → the neighbor-GPS time window defaults to 30 minutes.
    assert config.load(tmp_path / "nope.toml").inference_max_gap_minutes == 30.0


def test_inference_max_gap_minutes_override(tmp_path):
    cfg_path = tmp_path / "geosorter.toml"
    cfg_path.write_text("inference_max_gap_minutes = 45\n", encoding="utf-8")
    assert config.load(cfg_path).inference_max_gap_minutes == 45.0


def test_retain_hyperlapse_frames_default_true(tmp_path):
    # No config file → hyperlapse source frames are retained alongside the render.
    assert config.load(tmp_path / "nope.toml").retain_hyperlapse_frames is True


def test_retain_hyperlapse_frames_override_false(tmp_path):
    cfg_path = tmp_path / "geosorter.toml"
    cfg_path.write_text("retain_hyperlapse_frames = false\n", encoding="utf-8")
    assert config.load(cfg_path).retain_hyperlapse_frames is False


def test_hugin_bin_dir_default_none(tmp_path):
    # No config file → the optional Hugin binary dir is unset (PATH-only detection).
    assert config.load(tmp_path / "nope.toml").hugin_bin_dir is None


def test_hugin_bin_dir_override(tmp_path):
    cfg_path = tmp_path / "geosorter.toml"
    cfg_path.write_text("hugin_bin_dir = 'C:\\\\Program Files\\\\Hugin\\\\bin'\n", encoding="utf-8")
    cfg = config.load(cfg_path)
    assert cfg.hugin_bin_dir == Path("C:\\Program Files\\Hugin\\bin")


# --- Panorama-stitch tuning knobs (m-frontend-pano-ux) ---------------------- #
def test_stitch_knobs_defaults(tmp_path):
    # No config file → the smaller default canvas + both quality steps on.
    cfg = config.load(tmp_path / "nope.toml")
    assert cfg.stitch_canvas == "4000x2000"
    assert cfg.stitch_celeste is True
    assert cfg.stitch_optimise_lens is True


def test_stitch_knobs_override(tmp_path):
    cfg_path = tmp_path / "geosorter.toml"
    cfg_path.write_text(
        "stitch_canvas = '6000x3000'\n"
        "stitch_celeste = false\n"
        "stitch_optimise_lens = false\n",
        encoding="utf-8",
    )
    cfg = config.load(cfg_path)
    assert cfg.stitch_canvas == "6000x3000"
    assert cfg.stitch_celeste is False
    assert cfg.stitch_optimise_lens is False


@pytest.mark.parametrize("value", ["4000", "4000x", "x2000", "4000X2000", "wide", "4000 x 2000"])
def test_stitch_canvas_invalid_raises(tmp_path, value):
    cfg_path = tmp_path / "geosorter.toml"
    cfg_path.write_text(f"stitch_canvas = '{value}'\n", encoding="utf-8")
    with pytest.raises(ValueError):
        config.load(cfg_path)


# --- Organize-resilience knobs (m-organize-resilience) ---------------------- #
def test_resilience_knobs_defaults(tmp_path):
    # No config file → the upload-resilience knobs fall back to their defaults.
    cfg = config.load(tmp_path / "nope.toml")
    assert cfg.disk_margin_gb == 5.0
    assert cfg.copy_retry_attempts == 3
    assert cfg.copy_retry_backoff_s == 0.5
    assert cfg.extract_chunk_size == 500
    assert cfg.extract_max_failures == 3


def test_resilience_knobs_override(tmp_path):
    cfg_path = tmp_path / "geosorter.toml"
    cfg_path.write_text(
        "disk_margin_gb = 10\n"
        "copy_retry_attempts = 5\n"
        "copy_retry_backoff_s = 1.5\n"
        "extract_chunk_size = 250\n"
        "extract_max_failures = 2\n",
        encoding="utf-8",
    )
    cfg = config.load(cfg_path)
    assert cfg.disk_margin_gb == 10.0
    assert cfg.copy_retry_attempts == 5
    assert cfg.copy_retry_backoff_s == 1.5
    assert cfg.extract_chunk_size == 250
    assert cfg.extract_max_failures == 2


@pytest.mark.parametrize(
    "line",
    [
        "disk_margin_gb = -1\n",
        "copy_retry_attempts = 0\n",
        "copy_retry_backoff_s = -0.1\n",
        "extract_chunk_size = 0\n",
        "extract_max_failures = 0\n",
    ],
)
def test_resilience_knobs_invalid_raises(tmp_path, line):
    cfg_path = tmp_path / "geosorter.toml"
    cfg_path.write_text(line, encoding="utf-8")
    with pytest.raises(ValueError):
        config.load(cfg_path)


# --- Cache tiering (m-cache-tiering-safety) --------------------------------- #
def test_cache_tiering_defaults(tmp_path):
    # No config file → the derived cache defaults to the local platformdirs cache
    # dir, proxy_cache_dir is unset (resolves to library_root at use), 10 GB cap.
    cfg = config.load(tmp_path / "nope.toml")
    assert cfg.cache_dir == config.default_cache_dir()
    assert cfg.proxy_cache_dir is None
    assert cfg.cache_max_gb == 10.0


def test_cache_tiering_override(tmp_path):
    cache = tmp_path / "cache"
    proxy = tmp_path / "proxies"
    cfg_path = tmp_path / "geosorter.toml"
    cfg_path.write_text(
        f"cache_dir = '{cache.as_posix()}'\n"
        f"proxy_cache_dir = '{proxy.as_posix()}'\n"
        "cache_max_gb = 25\n",
        encoding="utf-8",
    )
    cfg = config.load(cfg_path)
    assert cfg.cache_dir == cache
    assert cfg.proxy_cache_dir == proxy
    assert cfg.cache_max_gb == 25.0


def test_cache_dir_must_be_absolute(tmp_path):
    cfg_path = tmp_path / "geosorter.toml"
    cfg_path.write_text("cache_dir = 'relative/cache'\n", encoding="utf-8")
    with pytest.raises(ValueError):
        config.load(cfg_path)


def test_cache_dir_must_not_be_inside_library_root(tmp_path):
    lib = tmp_path / "lib"
    cfg_path = tmp_path / "geosorter.toml"
    cfg_path.write_text(
        f"library_root = '{lib.as_posix()}'\n"
        f"cache_dir = '{(lib / 'cache').as_posix()}'\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        config.load(cfg_path)


def test_cache_max_gb_must_be_positive(tmp_path):
    cfg_path = tmp_path / "geosorter.toml"
    cfg_path.write_text("cache_max_gb = 0\n", encoding="utf-8")
    with pytest.raises(ValueError):
        config.load(cfg_path)


# --- Proxy pre-warm + proxy-tier cap (m-implement-proxy-prewarm-cap) -------- #
def test_warm_proxies_default_false(tmp_path):
    # No config file → proxy pre-warming is off (HEVC proxies stay lazy).
    assert config.load(tmp_path / "nope.toml").warm_proxies is False


def test_warm_proxies_override_true(tmp_path):
    cfg_path = tmp_path / "geosorter.toml"
    cfg_path.write_text("warm_proxies = true\n", encoding="utf-8")
    assert config.load(cfg_path).warm_proxies is True


def test_proxy_cache_max_gb_default_none(tmp_path):
    # No config file → the proxy tier is uncapped (today's never-evict behavior).
    assert config.load(tmp_path / "nope.toml").proxy_cache_max_gb is None


def test_proxy_cache_max_gb_override(tmp_path):
    cfg_path = tmp_path / "geosorter.toml"
    cfg_path.write_text("proxy_cache_max_gb = 50\n", encoding="utf-8")
    assert config.load(cfg_path).proxy_cache_max_gb == 50.0


@pytest.mark.parametrize("value", ["0", "-1", "-0.5"])
def test_proxy_cache_max_gb_non_positive_raises(tmp_path, value):
    cfg_path = tmp_path / "geosorter.toml"
    cfg_path.write_text(f"proxy_cache_max_gb = {value}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        config.load(cfg_path)


def test_resolve_proxy_cache_dir_defaults_to_raw_library_root(tmp_path):
    # Unset proxy_cache_dir -> the RAW library_root, NOT library_root.resolve(): the
    # stitch generator + serve route both call this helper, and on a mapped SMB drive
    # .resolve() rewrites Z:\ -> a UNC form that would make them disagree. A directory
    # symlink makes raw != resolved so this is a true red-green of "raw, not resolved".
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
        symlinked = Path(link).resolve() != link
    except (OSError, NotImplementedError):
        link, symlinked = real, False
    cfg = SimpleNamespace(proxy_cache_dir=None, library_root=link)
    assert config.resolve_proxy_cache_dir(cfg) == Path(link)  # raw form
    if symlinked:
        assert config.resolve_proxy_cache_dir(cfg) != Path(link).resolve()  # not resolved


def test_resolve_proxy_cache_dir_uses_explicit_when_set(tmp_path):
    cfg = SimpleNamespace(
        proxy_cache_dir=tmp_path / "proxies", library_root=tmp_path / "lib"
    )
    assert config.resolve_proxy_cache_dir(cfg) == tmp_path / "proxies"


def test_admin_password_hash_default_none(tmp_path):
    # No config file (and no key) -> auth is unconfigured (open app).
    assert config.load(tmp_path / "nope.toml").admin_password_hash is None


def test_admin_password_hash_loaded(tmp_path):
    cfg_path = tmp_path / "geosorter.toml"
    cfg_path.write_text(
        "admin_password_hash = 'pbkdf2_sha256$1$00$ff'\n", encoding="utf-8"
    )
    assert config.load(cfg_path).admin_password_hash == "pbkdf2_sha256$1$00$ff"


def test_admin_password_hash_blank_is_none(tmp_path):
    cfg_path = tmp_path / "geosorter.toml"
    cfg_path.write_text("admin_password_hash = ''\n", encoding="utf-8")
    assert config.load(cfg_path).admin_password_hash is None


def test_set_admin_password_hash_writes_and_loads(tmp_path):
    cfg_path = tmp_path / "geosorter.toml"
    config.write_starter(cfg_path)
    assert config.set_admin_password_hash(cfg_path, "pbkdf2_sha256$1$00$ab") is True
    assert config.load(cfg_path).admin_password_hash == "pbkdf2_sha256$1$00$ab"
    # Re-setting overwrites the existing key rather than appending a duplicate.
    assert config.set_admin_password_hash(cfg_path, "pbkdf2_sha256$1$00$cd") is True
    assert config.load(cfg_path).admin_password_hash == "pbkdf2_sha256$1$00$cd"
    keys = [
        ln.split("=", 1)[0].strip()
        for ln in cfg_path.read_text(encoding="utf-8").splitlines()
        if ln.split("=", 1)[0].strip() == "admin_password_hash"
    ]
    assert keys == ["admin_password_hash"]  # exactly one key line


def test_set_admin_password_hash_missing_file_is_noop(tmp_path):
    assert config.set_admin_password_hash(tmp_path / "nope.toml", "x") is False
