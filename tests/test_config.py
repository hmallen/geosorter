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
