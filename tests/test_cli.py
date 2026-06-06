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
    assert "rescan" in result.output


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


def test_undo_nothing_when_log_empty(tmp_path):
    cfg, _inbox, _library = _write_cfg_organize(tmp_path)
    result = CliRunner().invoke(cli, ["undo", "--yes", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "Nothing to undo" in result.output


def test_organize_then_undo_cli(tmp_path):
    # Full destructive CLI organize (--yes) then undo (--yes) round-trips the file.
    cfg, inbox, library = _write_cfg_organize(tmp_path)
    import shutil

    shutil.copy(MEDIA / "dji_photo.jpg", inbox / "DJI_0001.JPG")
    r1 = CliRunner().invoke(cli, ["organize", "--yes", "--config", str(cfg)])
    assert r1.exit_code == 0, r1.output
    assert not (inbox / "DJI_0001.JPG").exists()  # source auto-deleted

    r2 = CliRunner().invoke(cli, ["undo", "--yes", "--config", str(cfg)])
    assert r2.exit_code == 0, r2.output
    assert "restored:  1" in r2.output
    assert (inbox / "DJI_0001.JPG").exists()  # back in the inbox
    assert not any(p.is_file() for p in library.rglob("*"))  # library copy gone


def test_rescan_prunes_missing_cli(tmp_path):
    # Organize a photo, move it out of the library by hand, then rescan: the stale
    # index row is pruned so verify-library has nothing left to check.
    cfg, inbox, library = _write_cfg_organize(tmp_path)
    import shutil

    shutil.copy(MEDIA / "dji_photo.jpg", inbox / "DJI_0001.JPG")
    r1 = CliRunner().invoke(cli, ["organize", "--yes", "--config", str(cfg)])
    assert r1.exit_code == 0, r1.output
    dest = next(p for p in library.rglob("*") if p.is_file())
    dest.unlink()  # capture moved out of the library

    r2 = CliRunner().invoke(cli, ["rescan", "--yes", "--config", str(cfg)])
    assert r2.exit_code == 0, r2.output
    assert "pruned: 1" in r2.output

    r3 = CliRunner().invoke(cli, ["verify-library", "--config", str(cfg)])
    assert r3.exit_code == 0, r3.output
    assert "checked 0" in r3.output  # the pruned row is gone


def test_rescan_dry_run_writes_nothing_cli(tmp_path):
    cfg, inbox, library = _write_cfg_organize(tmp_path)
    import shutil

    shutil.copy(MEDIA / "dji_photo.jpg", inbox / "DJI_0001.JPG")
    CliRunner().invoke(cli, ["organize", "--yes", "--config", str(cfg)])
    next(p for p in library.rglob("*") if p.is_file()).unlink()

    r1 = CliRunner().invoke(cli, ["rescan", "--dry-run", "--config", str(cfg)])
    assert r1.exit_code == 0, r1.output
    assert "dry run" in r1.output.lower()
    assert "would prune: 1" in r1.output

    # Nothing was written: a second dry-run still finds the same stale row.
    r2 = CliRunner().invoke(cli, ["rescan", "--dry-run", "--config", str(cfg)])
    assert "would prune: 1" in r2.output


def _feature_src(tmp_path: Path) -> Path:
    """A GeoNames source dir = committed fixtures + an allCountries.txt sample."""
    import shutil

    src = tmp_path / "src"
    src.mkdir()
    for f in FIXTURES.iterdir():
        shutil.copy(f, src / f.name)
    shutil.copy(src / "allCountries_sample.txt", src / "allCountries.txt")
    return src


def test_bootstrap_with_features(tmp_path):
    cfg = _write_cfg(tmp_path)
    src = _feature_src(tmp_path)
    result = CliRunner().invoke(
        cli,
        ["bootstrap", "--from", str(src), "--no-download", "--features",
         "--config", str(cfg)],
    )
    assert result.exit_code == 0, result.output
    assert "Bootstrap complete" in result.output
    assert "3 features" in result.output


def test_geocode_test_prints_candidates_and_choice(tmp_path):
    from geosorter import geonames_loader

    src = _feature_src(tmp_path)
    gn_db = tmp_path / "geonames.db"
    geonames_loader.load(gn_db, src, features=True)
    cfg = tmp_path / "geosorter.toml"
    cfg.write_text(f"geonames_db_path = '{gn_db}'\n", encoding="utf-8")
    # Query right at Rocky Mountain National Park (40.4, -105.6); `--` lets the
    # negative longitude through as an argument rather than an option.
    result = CliRunner().invoke(
        cli, ["geocode-test", "--config", str(cfg), "--", "40.4", "-105.6"]
    )
    assert result.exit_code == 0, result.output
    assert "Rocky Mountain National Park" in result.output
    assert "nearest_feature" in result.output


def test_resolve_host_default_is_loopback_no_warn():
    from geosorter.cli import _resolve_host

    assert _resolve_host(None) == ("127.0.0.1", False)


def test_resolve_host_nonloopback_warns():
    from geosorter.cli import _resolve_host

    assert _resolve_host("0.0.0.0") == ("0.0.0.0", True)


def test_resolve_host_explicit_loopback_no_warn():
    from geosorter.cli import _resolve_host

    assert _resolve_host("127.0.0.1") == ("127.0.0.1", False)


def test_serve_binds_loopback_by_default(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path)
    captured = {}
    monkeypatch.setattr("geosorter.cli.api.create_app", lambda c: "APP")
    monkeypatch.setattr(
        "geosorter.cli.uvicorn.run",
        lambda app, host, port: captured.update(app=app, host=host, port=port),
    )
    result = CliRunner().invoke(cli, ["serve", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert captured == {"app": "APP", "host": "127.0.0.1", "port": 8000}
    assert "WARNING" not in result.output


def test_serve_explicit_host_warns(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path)
    monkeypatch.setattr("geosorter.cli.api.create_app", lambda c: "APP")
    monkeypatch.setattr("geosorter.cli.uvicorn.run", lambda app, host, port: None)
    result = CliRunner().invoke(
        cli, ["serve", "--host", "0.0.0.0", "--config", str(cfg)]
    )
    assert result.exit_code == 0, result.output
    assert "WARNING" in result.output
    assert "0.0.0.0" in result.output


def test_geocode_test_without_bootstrap_is_clean_error(tmp_path):
    cfg = tmp_path / "geosorter.toml"
    cfg.write_text(f"geonames_db_path = '{tmp_path / 'absent.db'}'\n", encoding="utf-8")
    result = CliRunner().invoke(
        cli, ["geocode-test", "--config", str(cfg), "--", "40.0", "-105.0"]
    )
    assert result.exit_code != 0
    assert "bootstrap" in result.output.lower()
