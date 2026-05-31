"""Configuration loading for geosorter.

Resolution order for the ``geosorter.toml`` location:

1. an explicit ``--config PATH`` (passed to :func:`load` / :func:`write_starter`)
2. the ``GEOSORTER_CONFIG`` environment variable
3. ``platformdirs.user_config_dir("geosorter")/geosorter.toml``

The two SQLite databases default to ``platformdirs.user_data_dir("geosorter")``
— always on local disk, never inside ``library_root``.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

import platformdirs

APP_NAME = "geosorter"

_STARTER_TEMPLATE = """\
# geosorter configuration

# Set these before running `geosorter organize` (added in a later task):
# inbox_path = 'D:\\drone\\inbox'
# library_root = 'Z:\\DroneLibrary'

# Local-disk databases (kept off library_root). Defaults shown.
index_db_path = '{index_db}'
geonames_db_path = '{geonames_db}'

# Spatial index for geocoding: 'rtree' (default) or 'columnar' (fallback).
spatial_index = 'rtree'
"""


@dataclass(frozen=True)
class Config:
    """Resolved geosorter configuration."""

    inbox_path: Path | None
    library_root: Path | None
    index_db_path: Path
    geonames_db_path: Path
    spatial_index: str = "rtree"


def default_data_dir() -> Path:
    # appauthor=False avoids the Windows "geosorter\geosorter" double nesting.
    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))


def default_config_path() -> Path:
    return (
        Path(platformdirs.user_config_dir(APP_NAME, appauthor=False))
        / "geosorter.toml"
    )


def resolve_config_path(explicit: str | Path | None = None) -> Path:
    """Resolve the config-file path per the documented precedence."""
    if explicit:
        return Path(explicit)
    env = os.environ.get("GEOSORTER_CONFIG")
    if env:
        return Path(env)
    return default_config_path()


def _opt_path(value: object) -> Path | None:
    if not value:
        return None
    return Path(str(value)).expanduser()


def load(path: str | Path | None = None) -> Config:
    """Load configuration, applying defaults for anything unset.

    A missing config file is not an error — defaults are returned (the two DB
    paths fall under :func:`default_data_dir`).
    """
    cfg_path = resolve_config_path(path)
    data: dict[str, object] = {}
    if cfg_path.exists():
        with open(cfg_path, "rb") as fh:
            data = tomllib.load(fh)

    data_dir = default_data_dir()
    index_db = data.get("index_db_path") or (data_dir / "index.db")
    geonames_db = data.get("geonames_db_path") or (data_dir / "geonames.db")
    spatial_index = str(data.get("spatial_index", "rtree"))
    if spatial_index not in ("rtree", "columnar"):
        raise ValueError(f"invalid spatial_index in config: {spatial_index!r}")

    return Config(
        inbox_path=_opt_path(data.get("inbox_path")),
        library_root=_opt_path(data.get("library_root")),
        index_db_path=Path(str(index_db)).expanduser(),
        geonames_db_path=Path(str(geonames_db)).expanduser(),
        spatial_index=spatial_index,
    )


def update_spatial_index(path: str | Path | None, value: str) -> bool:
    """Persist the effective ``spatial_index`` to an existing config file.

    Returns ``False`` (no-op) if the config file does not exist — bootstrap
    should not silently create one.
    """
    if value not in ("rtree", "columnar"):
        raise ValueError(f"invalid spatial_index: {value!r}")
    cfg_path = resolve_config_path(path)
    if not cfg_path.exists():
        return False
    lines = cfg_path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    found = False
    for line in lines:
        # Match the exact key token only — not a comment or a key that merely
        # starts with "spatial_index" (e.g. a hypothetical spatial_index_mode).
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key == "spatial_index":
            out.append(f"spatial_index = '{value}'")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"spatial_index = '{value}'")
    cfg_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return True


def write_starter(path: str | Path | None = None, *, overwrite: bool = False) -> Path:
    """Write a starter ``geosorter.toml`` and return its path."""
    cfg_path = resolve_config_path(path)
    if cfg_path.exists() and not overwrite:
        raise FileExistsError(cfg_path)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    data_dir = default_data_dir()
    content = _STARTER_TEMPLATE.format(
        index_db=data_dir / "index.db",
        geonames_db=data_dir / "geonames.db",
    )
    cfg_path.write_text(content, encoding="utf-8")
    return cfg_path
