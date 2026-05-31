"""CLI integration tests (click CliRunner, native tmp_path)."""

import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from geosorter.cli import cli

FIXTURES = Path(__file__).parent / "fixtures" / "geonames"
MEDIA = Path(__file__).parent / "fixtures" / "media"


def _write_cfg(tmp_path: Path) -> Path:
    cfg = tmp_path / "geosorter.toml"
    # single-quoted TOML literal strings — no escaping of Windows backslashes.
    cfg.write_text(
        f"index_db_path = '{tmp_path / 'index.db'}'\n"
        f"geonames_db_path = '{tmp_path / 'geonames.db'}'\n"
        "spatial_index = 'rtree'\n",
        encoding="utf-8",
    )
    return cfg


def test_help_lists_commands():
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    for cmd in ("init-config", "bootstrap", "version", "extract-test"):
        assert cmd in result.output


def test_extract_test_outputs_json():
    result = CliRunner().invoke(cli, ["extract-test", str(MEDIA / "dji_photo.jpg")])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["media_type"] == "photo"
    assert data["gps_source"] == "exif"
    assert data["codec"] is None
    assert data["lat"] == pytest.approx(43.0148385)


def test_init_config_writes_and_refuses_overwrite(tmp_path):
    runner = CliRunner()
    cfg = tmp_path / "starter.toml"
    r1 = runner.invoke(cli, ["init-config", "--config", str(cfg)])
    assert r1.exit_code == 0, r1.output
    assert cfg.exists()

    r2 = runner.invoke(cli, ["init-config", "--config", str(cfg)])
    assert r2.exit_code != 0  # refuses without --force

    r3 = runner.invoke(cli, ["init-config", "--config", str(cfg), "--force"])
    assert r3.exit_code == 0, r3.output


def test_bootstrap_from_fixtures_loads_db(tmp_path):
    cfg = _write_cfg(tmp_path)
    result = CliRunner().invoke(
        cli,
        ["bootstrap", "--from", str(FIXTURES), "--no-download", "--config", str(cfg)],
    )
    assert result.exit_code == 0, result.output
    assert "3 places" in result.output

    gn_db = tmp_path / "geonames.db"
    assert gn_db.exists()
    conn = sqlite3.connect(str(gn_db))
    try:
        assert conn.execute("SELECT COUNT(*) FROM geonames").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM geonames_rtree").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM country_info").fetchone()[0] == 2
    finally:
        conn.close()


def test_bootstrap_no_download_without_from_errors(tmp_path):
    cfg = _write_cfg(tmp_path)
    result = CliRunner().invoke(
        cli, ["bootstrap", "--no-download", "--config", str(cfg)]
    )
    assert result.exit_code != 0
    assert "--from" in result.output
