"""Desktop configuration and upgrade storage. Never infer paths from the cwd."""
from __future__ import annotations

from contextlib import closing

import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import tomlkit

from . import __version__, config, db


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, value: dict) -> None:
    atomic_write(path, json.dumps(value, indent=2))


def selected_config(explicit: str | Path | None, state_dir: Path) -> Path:
    if explicit:
        return Path(explicit).absolute()
    saved = read_json(state_dir / "desktop.json").get("config_path")
    return Path(saved) if saved else config.default_config_path()


def save_config(path: Path, changes: dict) -> None:
    doc = tomlkit.parse(path.read_text(encoding="utf-8")) if path.exists() else tomlkit.document()
    for key, value in changes.items():
        doc[key] = str(value) if isinstance(value, Path) else value
    content = tomlkit.dumps(doc)
    # Parse before replacing an existing user's file, and keep its exact old bytes.
    tomlkit.parse(content)
    if path.exists():
        shutil.copy2(path, path.with_suffix(".toml.bak"))
    atomic_write(path, content)


def checked_folder(value: str | Path, label: str, *, writable: bool = True) -> Path:
    path = Path(value).expanduser()
    if not str(value).strip() or not path.is_absolute():
        raise ValueError(f"Choose an absolute path for {label}.")
    if not path.is_dir():
        raise ValueError(f"{label} is unavailable. Connect the drive or choose an existing folder.")
    try:
        next(path.iterdir(), None)
        if writable:
            with tempfile.TemporaryFile(dir=path):
                pass
    except OSError as exc:
        raise ValueError(f"GeoSorter cannot access {label}. Check folder permissions.") from exc
    return path


def validate_folders(inbox: str | Path, library: str | Path, *, new: bool) -> tuple[Path, Path]:
    incoming = checked_folder(inbox, "incoming media folder")
    destination = checked_folder(library, "organized library folder")
    a, b = incoming.resolve(), destination.resolve()
    if a.is_relative_to(b) or b.is_relative_to(a):
        raise ValueError("The incoming folder and library must be separate; neither can contain the other.")
    if new and next(destination.iterdir(), None) is not None:
        raise ValueError("Choose an empty library folder, or use an existing GeoSorter configuration and catalog.")
    return incoming, destination


def index_version(path: Path) -> int | None:
    if not path.exists():
        return None
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("The catalog failed its integrity check. Restore a backup before continuing.")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "files" not in tables:
            raise ValueError("This is not a GeoSorter catalog. The existing library was not changed.")
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone() if "schema_version" in tables else None
        version = int(row[0]) if row and row[0] is not None else 1
        if version > db.SCHEMA_VERSION:
            raise ValueError("This catalog needs a newer version of GeoSorter. Install that version to continue.")
        return version


def prepare_upgrade(cfg: config.Config, cfg_path: Path, state_dir: Path) -> None:
    version = index_version(cfg.index_db_path)
    record = state_dir / "upgrade.json"
    previous = read_json(record)
    identity = str(cfg.index_db_path.absolute())
    if version is None or (previous.get("version") == __version__ and previous.get("catalog") == identity
                           and version == db.SCHEMA_VERSION):
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = state_dir / "backups" / stamp
    backup.mkdir(parents=True)
    with closing(sqlite3.connect(cfg.index_db_path.as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(backup / "index.db")) as target:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Catalog backup validation failed. Startup was stopped.")
    if cfg_path.exists():
        shutil.copy2(cfg_path, backup / "geosorter.toml")


def mark_upgrade(cfg: config.Config, state_dir: Path) -> None:
    save_json(state_dir / "upgrade.json", {"version": __version__, "catalog": str(cfg.index_db_path.absolute())})
