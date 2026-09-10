"""Shared, atomic GeoNames bootstrap for command-line and desktop clients."""
from __future__ import annotations

from contextlib import closing

import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from . import config, db, geonames_loader
from .desktop_config import save_config


def ready(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)) as conn:
            return all(conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
                       for table in ("geonames", "admin1_codes", "admin2_codes", "country_info"))
    except sqlite3.Error:
        return False


def features_ready(path: Path) -> bool:
    """Detect installed detailed places, including databases from older previews.

    Read the active database rather than the latest job or a browser flag: another
    optional-tool job, a restart, or a failed staging refresh must not lose this.
    """
    if not path.is_file():
        return False
    codes = sorted(geonames_loader.DEFAULT_FEATURE_CODES)
    placeholders = ",".join("?" for _ in codes)
    try:
        with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)) as conn:
            return conn.execute(
                "SELECT 1 FROM geonames WHERE feature_class IN ('L', 'T', 'H') "
                f"AND feature_code IN ({placeholders}) LIMIT 1", codes,
            ).fetchone() is not None
    except sqlite3.Error:
        return False


def run(cfg, *, config_path=None, source: Path | None = None, features=False, progress=None) -> dict:
    def report(phase, current="", done=0, total=0):
        if progress:
            progress(phase, current, done, total)

    if source is None:
        report("downloading")
        source = geonames_loader.download(
            config.default_data_dir() / "geonames-src", features=features,
            progress=lambda name, done, total: report("downloading", name, done, total),
            phase_progress=lambda phase: report(phase),
        )
    cfg.geonames_db_path.parent.mkdir(parents=True, exist_ok=True)
    # The catalog can live on a different volume from the download cache. Budget
    # staging separately, including the old detailed database copied on refresh.
    source_bytes = sum(p.stat().st_size for p in source.glob("*.txt"))
    previous_bytes = cfg.geonames_db_path.stat().st_size if cfg.geonames_db_path.exists() else 0
    needed = max(100 * 1024 * 1024, source_bytes * 4 + previous_bytes)
    if shutil.disk_usage(cfg.geonames_db_path.parent).free < needed:
        raise ValueError(f"Not enough free space to prepare place data. Free at least {needed // (1024 * 1024)} MiB on the place-data drive, then retry.")
    fd, temporary = tempfile.mkstemp(prefix="geonames-", suffix=".db", dir=cfg.geonames_db_path.parent)
    os.close(fd)
    stage = Path(temporary)
    effective = cfg.spatial_index
    try:
        conn = db.connect(stage, integrity_check=False)
        try:
            if effective == "rtree" and not db.probe_rtree(conn):
                effective = "columnar"
            # Preserve an existing detailed dataset when a city-only refresh is requested.
            if ready(cfg.geonames_db_path) and not features:
                with closing(sqlite3.connect(cfg.geonames_db_path.absolute().as_uri() + "?mode=ro", uri=True)) as old:
                    old.backup(conn)
        finally:
            conn.close()
        report("indexing")
        counts = geonames_loader.load(stage, source, spatial_index=effective, features=features)
        report("validating")
        if features and not features_ready(stage):
            raise ValueError("Detailed place data contains no usable parks, peaks or lakes. Please retry.")
        with closing(sqlite3.connect(stage)) as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or not ready(stage):
                raise ValueError("Downloaded place data failed validation. Please retry.")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA journal_mode=DELETE")
        # Consumers must be idle and have no persistent GeoNames connections here.
        if cfg.geonames_db_path.exists():
            with closing(sqlite3.connect(cfg.geonames_db_path)) as old:
                old.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                old.execute("PRAGMA journal_mode=DELETE")
        if config_path is not None and Path(config_path).exists():
            save_config(Path(config_path), {"spatial_index": effective})
        os.replace(stage, cfg.geonames_db_path)
        report("done")
        return {**counts, "spatial_index": effective}
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(str(stage) + suffix).unlink(missing_ok=True)
