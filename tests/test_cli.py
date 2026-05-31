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


def _write_cfg_organize(tmp_path: Path) -> tuple[Path, Path, Path]:
    import shutil

    from geosorter import geonames_loader

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    library = tmp_path / "library"
    gn_db = tmp_path / "geonames.db"
    geonames_loader.load(gn_db, FIXTURES, spatial_index="rtree")
    cfg = tmp_path / "geosorter.toml"
    cfg.write_text(
        f"inbox_path = '{inbox}'\n"
        f"library_root = '{library}'\n"
        f"index_db_path = '{tmp_path / 'index.db'}'\n"
        f"geonames_db_path = '{gn_db}'\n"
        "spatial_index = 'rtree'\n",
        encoding="utf-8",
    )
    return cfg, inbox, library


def test_help_lists_organize_verbs():
    result = CliRunner().invoke(cli, ["--help"])
    assert "organize" in result.output
    assert "verify-library" in result.output


def test_organize_requires_inbox_and_library(tmp_path):
    cfg = _write_cfg(tmp_path)  # no inbox_path / library_root
    result = CliRunner().invoke(cli, ["organize", "--config", str(cfg)])
    assert result.exit_code != 0
    assert "inbox_path" in result.output and "library_root" in result.output


def test_organize_first_run_gate_declined(tmp_path):
    # Gate runs BEFORE extraction, so this needs no ExifTool.
    cfg, inbox, library = _write_cfg_organize(tmp_path)
    import shutil

    shutil.copy(MEDIA / "dji_photo.jpg", inbox / "DJI_0001.JPG")
    result = CliRunner().invoke(cli, ["organize", "--config", str(cfg)], input="n\n")
    assert result.exit_code == 0, result.output
    assert "declined" in result.output.lower()
    assert (inbox / "DJI_0001.JPG").exists()  # nothing moved
    assert not library.exists() or not any(library.rglob("*"))


def test_organize_dry_run_real_photo(tmp_path):
    cfg, inbox, library = _write_cfg_organize(tmp_path)
    import shutil

    shutil.copy(MEDIA / "dji_photo.jpg", inbox / "DJI_0001.JPG")
    result = CliRunner().invoke(cli, ["organize", "--dry-run", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "DRY RUN" in result.output
    assert "organized:          1" in result.output
    assert (inbox / "DJI_0001.JPG").exists()  # dry-run moved nothing
    assert not library.exists() or not any(library.rglob("*"))


def test_organize_then_verify_library_cli(tmp_path):
    # Full destructive CLI organize (--yes) then verify-library on the result.
    cfg, inbox, library = _write_cfg_organize(tmp_path)
    import shutil

    shutil.copy(MEDIA / "dji_photo.jpg", inbox / "DJI_0001.JPG")
    r1 = CliRunner().invoke(cli, ["organize", "--yes", "--config", str(cfg)])
    assert r1.exit_code == 0, r1.output
    assert "organized:          1" in r1.output
    assert not (inbox / "DJI_0001.JPG").exists()  # source auto-deleted

    r2 = CliRunner().invoke(cli, ["verify-library", "--config", str(cfg)])
    assert r2.exit_code == 0, r2.output
    assert "checked 1, ok 1" in r2.output
