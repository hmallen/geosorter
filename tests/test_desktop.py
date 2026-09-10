from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from geosorter import auth, bootstrap, config, db, geonames_loader
from geosorter.desktop import DesktopController, create_desktop_app
from geosorter.desktop_config import (index_version, prepare_upgrade, save_config,
                                     selected_config, validate_folders)
from geosorter.desktop_launcher import InstanceLock, listening_socket

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "geonames"
ORIGIN = "http://127.0.0.1:9876"


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GEOSORTER_CONFIG", raising=False)
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(config, "default_data_dir", lambda: data)
    monkeypatch.setattr(config, "default_cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(config, "default_config_path", lambda: tmp_path / "settings.toml")
    (tmp_path / "incoming").mkdir()
    (tmp_path / "library").mkdir()
    return tmp_path


def controller(paths, **kwargs):
    return DesktopController(state_dir=paths / "desktop", checks=lambda: [{"name": "tools", "ok": True, "message": "Ready"}], **kwargs)


def configure(c, paths):
    return c.configure(str(paths / "incoming"), str(paths / "library"), apply=True)


def finish(c):
    if c.worker:
        c.worker.join(timeout=10)
        assert not c.worker.is_alive()
    if c.jobs:
        c.jobs.shutdown()


def test_first_run_no_library_app_or_media_changes(paths):
    c = controller(paths)
    assert c.library is None
    assert c.snapshot()["state"] == "setup"
    (paths / "incoming" / "keep.jpg").write_bytes(b"original")
    configure(c, paths)
    assert c.library is None
    assert (paths / "incoming" / "keep.jpg").read_bytes() == b"original"
    assert not list((paths / "library").iterdir())
    bootstrap.run(c.cfg, source=FIXTURES)
    c.recheck()
    assert c.snapshot()["state"] == "ready"
    finish(c)


def test_configuration_preserves_comments_and_backup(paths):
    file = paths / "settings.toml"
    original = '# keep me\nfeature_proximity_km = 8.0 # tuned\ninbox_path = "old"\n'
    file.write_text(original)
    save_config(file, {"inbox_path": "C:\\O'Brien\\日本"})
    text = file.read_text(encoding="utf-8")
    assert "# keep me" in text and "# tuned" in text
    assert config.load(file).inbox_path == Path("C:\\O'Brien\\日本")
    assert file.with_suffix(".toml.bak").read_text() == original


@pytest.mark.parametrize("kind", ["same", "nested", "nonempty", "missing"])
def test_reject_bad_folder_pairs(paths, kind):
    inbox, library = paths / "incoming", paths / "library"
    if kind == "same":
        library = inbox
    elif kind == "nested":
        library = inbox / "child"
        library.mkdir()
    elif kind == "nonempty":
        (library / "existing.jpg").write_bytes(b"keep")
    else:
        library = paths / "missing"
    with pytest.raises(ValueError):
        validate_folders(inbox, library, new=True)


def test_selected_config_ignores_developer_environment(paths, monkeypatch):
    monkeypatch.setenv("GEOSORTER_CONFIG", "bad.toml")
    assert selected_config(None, paths) == config.default_config_path()
    assert selected_config(paths / "explicit.toml", paths) == paths / "explicit.toml"


def test_config_write_failure_does_not_leave_new_catalog(paths, monkeypatch):
    from geosorter import desktop
    c = controller(paths)
    monkeypatch.setattr(desktop, "save_config", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        configure(c, paths)
    assert not c.cfg.index_db_path.exists()
    assert not c.config_path.exists()


def test_missing_catalog_never_replaced(paths):
    save_config(config.default_config_path(), {"inbox_path": str(paths / "incoming"), "library_root": str(paths / "library")})
    c = controller(paths)
    assert c.snapshot()["state"] == "recovery"
    assert "catalog is missing" in c.error
    assert not c.cfg.index_db_path.exists()


def test_malformed_configuration_is_recovery(paths):
    config.default_config_path().write_text("inbox_path = [broken")
    c = controller(paths)
    assert c.snapshot()["state"] == "recovery"
    assert c.library is None


def test_disconnected_library_retains_config(paths):
    c = controller(paths)
    configure(c, paths)
    bootstrap.run(c.cfg, source=FIXTURES)
    c.recheck()
    original = c.config_path.read_bytes()
    (paths / "library").rename(paths / "offline")
    assert c.snapshot()["state"] == "recovery"
    assert c.config_path.read_bytes() == original
    finish(c)


def test_bootstrap_failure_preserves_database(paths, monkeypatch):
    cfg = config.load()
    bootstrap.run(cfg, source=FIXTURES)
    original = cfg.geonames_db_path.read_bytes()
    monkeypatch.setattr(geonames_loader, "load", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        bootstrap.run(cfg, source=FIXTURES)
    assert cfg.geonames_db_path.read_bytes() == original
    assert bootstrap.ready(cfg.geonames_db_path)


def test_duplicate_job_and_quit_drain(paths):
    started, release, stopped = threading.Event(), threading.Event(), threading.Event()
    def run(cfg, **kwargs):
        started.set()
        assert release.wait(10)
        bootstrap.run(cfg, source=FIXTURES)
    c = controller(paths, bootstrap_fn=run, on_quit=stopped.set)
    configure(c, paths)
    job = c.start_job("cities")
    assert started.wait(3)
    assert c.start_job("cities")["job_id"] == job["job_id"]
    assert c.request_quit()["busy"]
    c.request_quit(True)
    assert not stopped.is_set()
    with pytest.raises(ValueError, match="busy"):
        c.start_job("features")
    release.set()
    assert stopped.wait(10)
    assert c.closed


def test_job_interrupted_state_survives_restart(paths):
    state = paths / "desktop"
    state.mkdir()
    (state / "setup-job.json").write_text(json.dumps({"job_id": "old", "kind": "cities", "state": "running", "phase": "indexing"}))
    c = controller(paths)
    assert c.job["state"] == "interrupted"
    assert c.active() == []


def client(c):
    result = TestClient(create_desktop_app(c, origin=ORIGIN), base_url=ORIGIN)
    result.headers["Origin"] = ORIGIN
    return result


def login_desktop(client, c):
    assert client.post("/api/desktop/session", json={"token": c.secret}).status_code == 200


def test_desktop_session_host_origin_and_diagnostics(paths):
    c = controller(paths)
    web = client(c)
    assert web.get("/api/desktop/status").status_code == 401
    assert web.post("/api/desktop/session", json={"token": "wrong"}).status_code == 401
    login_desktop(web, c)
    assert web.get("/api/desktop/status").status_code == 200
    for headers in ({"Origin": "https://evil.invalid"}, {"Host": "evil.invalid"}):
        assert web.post("/api/desktop/configure", json={}, headers=headers).status_code == 403
    assert web.post("/api/desktop/choose", json={"kind": "folder"}).json() == {"path": None}
    payload = web.get("/api/desktop/diagnostics").text
    assert c.secret not in payload and c.cookie not in payload and str(paths) not in payload


def test_existing_admin_password_required_for_settings(paths):
    c = controller(paths)
    configure(c, paths)
    save_config(c.config_path, {"admin_password_hash": auth.hash_password("test-password")})
    c.recheck()
    web = client(c)
    login_desktop(web, c)
    assert web.post("/api/desktop/configure", json={"inbox_path": str(paths / "incoming"), "library_root": str(paths / "library")}).status_code == 401
    token = web.post("/api/desktop/login", json={"password": "test-password"}).json()["token"]
    web.headers["Authorization"] = "Bearer " + token
    assert web.post("/api/desktop/retry", json={}).status_code == 200


def test_forward_schema_refused_and_upgrade_backup(paths):
    c = controller(paths)
    configure(c, paths)
    prepare_upgrade(c.cfg, c.config_path, c.state_dir)
    backups = list((c.state_dir / "backups").glob("*/index.db"))
    assert len(backups) == 1
    with sqlite3.connect(c.cfg.index_db_path) as conn:
        conn.execute("UPDATE schema_version SET version=?", (db.SCHEMA_VERSION + 1,))
    with pytest.raises(ValueError, match="newer"):
        index_version(c.cfg.index_db_path)
    with sqlite3.connect(c.cfg.index_db_path) as conn:
        with pytest.raises(ValueError, match="newer"):
            db.init_index_schema(conn)
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == db.SCHEMA_VERSION + 1


@pytest.mark.skipif(os.name != "nt", reason="Windows desktop instance mutex")
def test_socket_fallback_and_instance_lock(paths):
    first = listening_socket(0)
    second = listening_socket(first.getsockname()[1])
    try:
        assert first.getsockname()[1] != second.getsockname()[1]
    finally:
        first.close()
        second.close()
    one, two = InstanceLock(paths), InstanceLock(paths)
    try:
        assert one.acquire()
        assert not two.acquire()
    finally:
        one.close()
    assert two.acquire()
    two.close()


def test_hugin_validation_executes_every_tool_and_rejects_broken_binary(monkeypatch):
    from types import SimpleNamespace
    from geosorter import desktop
    tools = {"pto_gen": "pto_gen.exe", "nona": "nona.exe", "enblend": "enblend.exe"}
    monkeypatch.setattr(desktop.derived, "find_hugin", lambda path: tools)
    calls = []
    def run(args, **kwargs):
        calls.append(args[0])
        return SimpleNamespace(returncode=0, stdout="Usage: test", stderr="")
    monkeypatch.setattr(desktop.subprocess, "run", run)
    assert desktop.validate_hugin("example") == tools
    assert calls == list(tools.values())
    monkeypatch.setattr(desktop.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=3221225781, stdout="", stderr=""))
    with pytest.raises(ValueError, match="Reinstall Hugin"):
        desktop.validate_hugin("example")
