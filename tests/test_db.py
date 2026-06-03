"""Tests for the geosorter SQLite foundation (db module)."""

from geosorter import db

# A v1 (B8-era) ``files`` table — the current schema MINUS the three columns B9a
# adds. Used to build a synthetic pre-migration DB and prove the v1->v2 upgrade.
_V1_FILES_DDL = """
CREATE TABLE files (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    geonameid         INTEGER,
    place_string      TEXT,
    dest_path         TEXT NOT NULL UNIQUE,
    filename          TEXT NOT NULL,
    media_type        TEXT NOT NULL,
    capture_ts_utc    TEXT,
    capture_ts_local  TEXT,
    local_date        TEXT,
    lat               REAL,
    lon               REAL,
    gps_source        TEXT,
    geocode_confidence TEXT,
    tz_ambiguous      INTEGER NOT NULL DEFAULT 0,
    codec             TEXT,
    width             INTEGER,
    height            INTEGER,
    duration_s        REAL,
    sha256            TEXT NOT NULL,
    status            TEXT NOT NULL,
    batch_id          TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE schema_version (
    version    INTEGER NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_B9A_NEW_COLUMNS = {"capture_kind", "frame_count", "star_rating"}


def _make_v1_index_db(path):
    """Create a synthetic v1 index DB with one real ``files`` row at version 1."""
    conn = db.connect(path)
    conn.executescript(_V1_FILES_DDL)
    conn.execute(
        "INSERT INTO files (dest_path, filename, media_type, sha256, status, lat, lon) "
        "VALUES ('C:/lib/x.jpg', 'x.jpg', 'photo', 'abc123', 'organized', 4.8, -75.6)"
    )
    conn.execute("INSERT INTO schema_version (version) VALUES (1)")
    conn.commit()
    return conn


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_connect_sets_pragmas(tmp_path):
    conn = db.connect(tmp_path / "x.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1  # ON
    finally:
        conn.close()


def test_connect_runs_integrity_check_on_fresh_db(tmp_path):
    # connect() runs PRAGMA integrity_check and raises on failure; a fresh DB
    # must open without raising.
    conn = db.connect(tmp_path / "fresh.db")
    conn.close()


def test_probe_rtree_true_on_this_platform(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    try:
        assert db.probe_rtree(conn) is True
    finally:
        conn.close()


def test_init_index_schema_creates_tables(tmp_path):
    conn = db.connect(tmp_path / "idx.db")
    try:
        db.init_index_schema(conn)
        names = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "files",
            "file_companions",
            "moves",
            "geocode_cache",
            "codec_stats",
            "schema_version",
        } <= names
    finally:
        conn.close()


def test_init_index_schema_is_idempotent(tmp_path):
    conn = db.connect(tmp_path / "idx2.db")
    try:
        db.init_index_schema(conn)
        db.init_index_schema(conn)  # second call must not raise
    finally:
        conn.close()


def test_migrate_v1_to_v2_adds_columns_losslessly(tmp_path):
    conn = _make_v1_index_db(tmp_path / "v1.db")
    try:
        # precondition: v1 lacks the new columns and is at version 1
        assert not (_B9A_NEW_COLUMNS & _columns(conn, "files"))
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 1

        db.init_index_schema(conn)  # runs the migration on the existing v1 DB

        assert _B9A_NEW_COLUMNS <= _columns(conn, "files")
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 2
        # the pre-existing row survives, new columns read as NULL
        row = conn.execute(
            "SELECT sha256, capture_kind, frame_count, star_rating FROM files"
        ).fetchone()
        assert row[0] == "abc123"
        assert row[1] is None and row[2] is None and row[3] is None
    finally:
        conn.close()


def test_migrate_v1_to_v2_is_idempotent(tmp_path):
    conn = _make_v1_index_db(tmp_path / "v1_idem.db")
    try:
        db.init_index_schema(conn)
        db.init_index_schema(conn)  # second run must be a clean no-op
        assert _B9A_NEW_COLUMNS <= _columns(conn, "files")
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 2
        # exactly one schema_version row (no duplicate stamping)
        assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1
    finally:
        conn.close()


def test_fresh_install_is_v2_with_new_columns(tmp_path):
    conn = db.connect(tmp_path / "fresh_v2.db")
    try:
        db.init_index_schema(conn)
        assert _B9A_NEW_COLUMNS <= _columns(conn, "files")
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 2
    finally:
        conn.close()


def test_init_geonames_schema_creates_tables_and_rtree(tmp_path):
    conn = db.connect(tmp_path / "gn.db")
    try:
        db.init_geonames_schema(conn, spatial_index="rtree")
        tnames = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "geonames",
            "admin1_codes",
            "admin2_codes",
            "country_info",
            "schema_version",
        } <= tnames
        assert "geonames_rtree" in tnames
    finally:
        conn.close()


def test_init_geonames_schema_columnar_fallback(tmp_path):
    conn = db.connect(tmp_path / "gn_col.db")
    try:
        db.init_geonames_schema(conn, spatial_index="columnar")
        idx = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        # columnar mode builds a covering (lat, lon) index instead of the rtree
        assert any("lat" in name.lower() for name in idx)
        tnames = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "geonames_rtree" not in tnames
    finally:
        conn.close()
