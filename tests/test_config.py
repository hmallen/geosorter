"""Tests for config loading and the starter writer."""

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
