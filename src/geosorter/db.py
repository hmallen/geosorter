"""SQLite foundation for geosorter.

Two databases, both kept on local disk (never inside a possibly-NAS
``library_root``):

* **index DB** — ``files``, ``file_companions``, ``moves``, ``geocode_cache``,
  ``codec_stats``, ``duplicates``, ``favorites`` (the operational index +
  crash-safe move log + duplicate-review backlog + content-hash favorites).
* **geonames DB** — ``geonames`` (+ optional ``geonames_rtree``),
  ``admin1_codes``, ``admin2_codes``, ``country_info`` (static reference data).

No ORM — raw ``sqlite3``. Connections are per-thread (never shared across
threads); callers open their own connection.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 5  # v4->v5: duplicates + favorites tables (new tables only, no ALTERs)

_INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    geonameid         INTEGER,                       -- canonical place key (stable)
    place_string      TEXT,                          -- display-only, may drift
    dest_path         TEXT NOT NULL UNIQUE,          -- absolute path in library
    filename          TEXT NOT NULL,
    media_type        TEXT NOT NULL,                 -- 'photo' | 'video'
    capture_kind      TEXT,                          -- NULL=normal | 'hyperlapse' | 'panorama' (B10/B12)
    frame_count       INTEGER,                       -- # source frames for hyperlapse/panorama (B10/B12)
    star_rating       INTEGER,                       -- DJI in-app star rating, from MISC catalog (B11)
    stitch_status     TEXT,                          -- panorama hero: NULL=none|'pending'|'ok'|'failed' (B13)
    stitch_projection TEXT,                          -- panorama hero projection: NULL|'equirectangular'|'flat' (v4)
    capture_ts_utc    TEXT,                          -- ISO 8601 UTC
    capture_ts_local  TEXT,                          -- ISO 8601 with local offset
    local_date        TEXT,                          -- YYYY-MM-DD (GPS-derived local)
    lat               REAL,
    lon               REAL,
    gps_source        TEXT,                          -- 'exif'|'srt'|'srt_partial'|'inferred'|'none'
    geocode_confidence TEXT,                          -- 'exact'|'nearest_feature'|'nearest_city'|'fallback'
    tz_ambiguous      INTEGER NOT NULL DEFAULT 0,
    codec             TEXT,                          -- 'h264'|'h265'|NULL (photos)
    width             INTEGER,
    height            INTEGER,
    duration_s        REAL,
    sha256            TEXT NOT NULL,
    status            TEXT NOT NULL,                 -- 'organized'|'quarantined'
    batch_id          TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_files_geonameid ON files(geonameid);
CREATE INDEX IF NOT EXISTS idx_files_local_date ON files(local_date);
CREATE INDEX IF NOT EXISTS idx_files_latlon ON files(lat, lon);
CREATE INDEX IF NOT EXISTS idx_files_sha256 ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_files_status_latlon ON files(status, lat, lon);

CREATE TABLE IF NOT EXISTS file_companions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    primary_file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    dest_path       TEXT NOT NULL,
    companion_type  TEXT NOT NULL                    -- 'dng'|'lrf'|'srt'|'other'
);
CREATE INDEX IF NOT EXISTS idx_companions_primary ON file_companions(primary_file_id);

CREATE TABLE IF NOT EXISTS moves (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id       INTEGER REFERENCES files(id),
    batch_id      TEXT NOT NULL,
    source_path   TEXT NOT NULL,
    dest_path     TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    dest_sha256   TEXT,
    status        TEXT NOT NULL
                  CHECK (status IN ('pending','copy_verified','source_deleted','failed','aborted')),
    started_at    TEXT,
    completed_at  TEXT,
    UNIQUE (source_path, source_sha256)
);
CREATE INDEX IF NOT EXISTS idx_moves_batch ON moves(batch_id);
CREATE INDEX IF NOT EXISTS idx_moves_sha_status ON moves(source_sha256, status);
CREATE INDEX IF NOT EXISTS idx_moves_dest_path ON moves(dest_path);

CREATE TABLE IF NOT EXISTS geocode_cache (
    lat_key            REAL NOT NULL,
    lon_key            REAL NOT NULL,
    geonameid          INTEGER,
    place_string       TEXT,
    feature_class      TEXT,
    geocode_confidence TEXT,
    cached_at          TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (lat_key, lon_key)
);

CREATE TABLE IF NOT EXISTS codec_stats (
    batch_id      TEXT,
    h264_count    INTEGER NOT NULL DEFAULT 0,
    h265_count    INTEGER NOT NULL DEFAULT 0,
    unknown_count INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS duplicates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT NOT NULL UNIQUE,      -- absolute inbox path of the primary
    sha256 TEXT NOT NULL,
    companion_paths TEXT NOT NULL DEFAULT '[]',  -- JSON array of absolute paths
    matched_file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
    matched_dest_path TEXT,                -- snapshot for display if the row dies
    batch_id TEXT,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS favorites (
    sha256 TEXT PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_GEONAMES_SCHEMA = """
CREATE TABLE IF NOT EXISTS geonames (
    geonameid     INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    ascii_name    TEXT,                              -- filesystem-safe display name
    lat           REAL NOT NULL,
    lon           REAL NOT NULL,
    feature_class TEXT,                              -- 'P'|'L'|'T'|'H'...
    feature_code  TEXT,
    country_code  TEXT,
    admin1_code   TEXT,
    admin2_code   TEXT,
    population    INTEGER NOT NULL DEFAULT 0,
    timezone      TEXT
);

CREATE TABLE IF NOT EXISTS admin1_codes (
    code       TEXT PRIMARY KEY,                     -- e.g. 'US.CO'
    name       TEXT,
    ascii_name TEXT,
    geonameid  INTEGER
);

CREATE TABLE IF NOT EXISTS admin2_codes (
    code       TEXT PRIMARY KEY,                     -- e.g. 'US.CO.013'
    name       TEXT,
    ascii_name TEXT,
    geonameid  INTEGER
);

CREATE TABLE IF NOT EXISTS country_info (
    country_code TEXT PRIMARY KEY,                   -- ISO 'US'
    country_name TEXT,
    geonameid    INTEGER
);

CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_RTREE_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS geonames_rtree "
    "USING rtree(id, min_lat, max_lat, min_lon, max_lon)"
)

_COLUMNAR_DDL = "CREATE INDEX IF NOT EXISTS idx_geonames_latlon ON geonames(lat, lon)"


#: How long a writer waits for a competing write lock before SQLITE_BUSY.
#: WAL allows readers alongside one writer, but a SECOND writer still gets
#: "database is locked" — and background jobs (organize/retag/rescan/...) do
#: write the index DB concurrently with request-path writes (geocode_cache).
BUSY_TIMEOUT_MS = 10_000


def connect(path: str | Path, *, integrity_check: bool = True) -> sqlite3.Connection:
    """Open a SQLite connection with the project's standard PRAGMAs.

    Sets WAL journaling, ``synchronous=NORMAL``, ``foreign_keys=ON`` and a
    ``busy_timeout`` (without it a second concurrent writer fails with
    ``database is locked`` immediately instead of waiting its turn). When
    ``integrity_check`` is true (default) runs ``PRAGMA integrity_check`` and
    raises :class:`sqlite3.DatabaseError` if the result is not ``ok``.

    The parent directory is created if missing. The returned connection is
    intended for use on a single thread only.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    if integrity_check:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            conn.close()
            raise sqlite3.DatabaseError(f"integrity_check failed for {path}: {result}")
    return conn


def probe_rtree(conn: sqlite3.Connection) -> bool:
    """Return ``True`` if the SQLite R-tree module is available.

    Attempts to create a throwaway R-tree virtual table in the ``temp`` schema
    and drops it again. Returns ``False`` if the module is not compiled in.
    """
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE temp._rtree_probe USING rtree(id, mn, mx)"
        )
    except sqlite3.OperationalError:
        return False
    conn.execute("DROP TABLE temp._rtree_probe")
    return True


def _stamp_version(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO schema_version(version) "
        "SELECT ? WHERE NOT EXISTS (SELECT 1 FROM schema_version)",
        (SCHEMA_VERSION,),
    )


# Columns added after the original v1 ``files`` table, applied to existing
# installs via ``ALTER TABLE``. A fresh install gets them straight from
# ``_INDEX_SCHEMA`` (so the migration is a no-op there). Maps column -> DDL type;
# all are nullable so existing rows read as ``NULL``.
_INDEX_MIGRATIONS: dict[str, str] = {
    "capture_kind": "TEXT",
    "frame_count": "INTEGER",
    "star_rating": "INTEGER",
    "stitch_status": "TEXT",
    "stitch_projection": "TEXT",
}


def migrate_index_schema(conn: sqlite3.Connection) -> None:
    """Bring an existing index DB up to ``SCHEMA_VERSION`` (idempotent).

    ``_INDEX_SCHEMA`` only ``CREATE TABLE IF NOT EXISTS``-es, so it never adds a
    column to a pre-existing ``files`` table. This adds each missing column via
    ``ALTER TABLE`` (guarded against a concurrent writer that already added it),
    then stamps ``schema_version`` to the new version — but **only after** every
    target column is confirmed present, so a crash mid-migration never leaves a
    version stamp ahead of the actual columns. Cheap to re-run (a ``PRAGMA
    table_info`` read + no-op) for the many callers of ``init_index_schema``.
    """
    existing = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
    for column, decl in _INDEX_MIGRATIONS.items():
        if column in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE files ADD COLUMN {column} {decl}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise  # a real error, not a lost ADD-COLUMN race
    final = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
    if _INDEX_MIGRATIONS.keys() <= final:
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
    conn.commit()


def init_index_schema(conn: sqlite3.Connection) -> None:
    """Create the index-DB tables (idempotent) and migrate to the current version."""
    conn.executescript(_INDEX_SCHEMA)
    _stamp_version(conn)
    migrate_index_schema(conn)
    conn.commit()


def init_geonames_schema(
    conn: sqlite3.Connection, *, spatial_index: str = "rtree"
) -> None:
    """Create the geonames-DB tables (idempotent).

    ``spatial_index`` selects the nearest-neighbour acceleration structure:
    ``"rtree"`` creates the ``geonames_rtree`` virtual table; ``"columnar"``
    creates a covering ``(lat, lon)`` index instead (fallback when the R-tree
    module is unavailable).
    """
    if spatial_index not in ("rtree", "columnar"):
        raise ValueError(f"unknown spatial_index: {spatial_index!r}")
    conn.executescript(_GEONAMES_SCHEMA)
    if spatial_index == "rtree":
        conn.execute(_RTREE_DDL)
    else:
        conn.execute(_COLUMNAR_DDL)
    _stamp_version(conn)
    conn.commit()
