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
_B13_NEW_COLUMN = {"stitch_status"}  # added by the v2->v3 migration
_V4_NEW_COLUMN = {"stitch_projection"}  # added by the v3->v4 migration
_V6_NEW_COLUMN = {"no_gps_hidden"}  # added by the v5->v6 migration

# A v2 (B9a-era) ``files`` table — the current schema MINUS the B13 stitch_status
# column. Used to prove the v2->v3 upgrade adds stitch_status losslessly.
_V2_FILES_DDL = """
CREATE TABLE files (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    geonameid         INTEGER,
    place_string      TEXT,
    dest_path         TEXT NOT NULL UNIQUE,
    filename          TEXT NOT NULL,
    media_type        TEXT NOT NULL,
    capture_kind      TEXT,
    frame_count       INTEGER,
    star_rating       INTEGER,
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


def _make_v2_index_db(path):
    """Create a synthetic v2 index DB (B9a columns, no stitch_status) at version 2."""
    conn = db.connect(path)
    conn.executescript(_V2_FILES_DDL)
    conn.execute(
        "INSERT INTO files (dest_path, filename, media_type, sha256, status, lat, lon, "
        "capture_kind, frame_count) "
        "VALUES ('C:/lib/pano.jpg', 'pano.jpg', 'photo', 'def456', 'organized', "
        "4.8, -75.6, 'panorama', 35)"
    )
    conn.execute("INSERT INTO schema_version (version) VALUES (2)")
    conn.commit()
    return conn


# A v3 (B13-era) ``files`` table — the v2 DDL PLUS stitch_status, MINUS the v4
# stitch_projection column. Used to prove the v3->v4 upgrade adds it losslessly.
_V3_FILES_DDL = _V2_FILES_DDL.replace(
    "    star_rating       INTEGER,\n",
    "    star_rating       INTEGER,\n    stitch_status     TEXT,\n",
)


def _make_v3_index_db(path):
    """Create a synthetic v3 index DB (B13 columns, no stitch_projection) at version 3."""
    conn = db.connect(path)
    conn.executescript(_V3_FILES_DDL)
    conn.execute(
        "INSERT INTO files (dest_path, filename, media_type, sha256, status, lat, lon, "
        "capture_kind, frame_count, stitch_status) "
        "VALUES ('C:/lib/pano.jpg', 'pano.jpg', 'photo', 'def456', 'organized', "
        "4.8, -75.6, 'panorama', 12, 'ok')"
    )
    conn.execute("INSERT INTO schema_version (version) VALUES (3)")
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
        # A second concurrent writer must wait instead of failing immediately
        # with "database is locked" (background jobs + request-path writes).
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == db.BUSY_TIMEOUT_MS
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
            "duplicates",
            "favorites",
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

        assert (
            _B9A_NEW_COLUMNS | _B13_NEW_COLUMN | _V4_NEW_COLUMN | _V6_NEW_COLUMN
        ) <= _columns(conn, "files")
        # migrate_index_schema stamps the CURRENT SCHEMA_VERSION once every target
        # column is present, so an old v1 DB jumps straight to the latest (now 6).
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 6
        # the pre-existing row survives, new columns read as NULL
        row = conn.execute(
            "SELECT sha256, capture_kind, frame_count, star_rating, stitch_status FROM files"
        ).fetchone()
        assert row[0] == "abc123"
        assert row[1] is None and row[2] is None and row[3] is None and row[4] is None
    finally:
        conn.close()


def test_migrate_v1_to_v2_is_idempotent(tmp_path):
    conn = _make_v1_index_db(tmp_path / "v1_idem.db")
    try:
        db.init_index_schema(conn)
        db.init_index_schema(conn)  # second run must be a clean no-op
        assert _B9A_NEW_COLUMNS <= _columns(conn, "files")
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 6
        # exactly one schema_version row (no duplicate stamping)
        assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1
    finally:
        conn.close()


def test_migrate_v2_to_v3_adds_stitch_status_losslessly(tmp_path):
    conn = _make_v2_index_db(tmp_path / "v2.db")
    try:
        # precondition: v2 has the B9a columns but not stitch_status, at version 2
        assert _B9A_NEW_COLUMNS <= _columns(conn, "files")
        assert not (_B13_NEW_COLUMN & _columns(conn, "files"))
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 2

        db.init_index_schema(conn)  # runs the v2->v3 migration on the existing DB

        assert _B13_NEW_COLUMN <= _columns(conn, "files")
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 6
        # the pre-existing panorama row survives, stitch_status reads as NULL
        row = conn.execute(
            "SELECT sha256, capture_kind, frame_count, stitch_status FROM files"
        ).fetchone()
        assert row[0] == "def456"
        assert row[1] == "panorama" and row[2] == 35 and row[3] is None
    finally:
        conn.close()


def test_migrate_v3_to_v4_adds_stitch_projection_losslessly(tmp_path):
    conn = _make_v3_index_db(tmp_path / "v3.db")
    try:
        # precondition: v3 has stitch_status but not stitch_projection, at version 3
        assert _B13_NEW_COLUMN <= _columns(conn, "files")
        assert not (_V4_NEW_COLUMN & _columns(conn, "files"))
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 3

        db.init_index_schema(conn)  # runs the v3->v4 migration on the existing DB

        assert _V4_NEW_COLUMN <= _columns(conn, "files")
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 6
        # the pre-existing panorama row survives, stitch_projection reads as NULL
        row = conn.execute(
            "SELECT sha256, capture_kind, stitch_status, stitch_projection FROM files"
        ).fetchone()
        assert row[0] == "def456"
        assert row[1] == "panorama" and row[2] == "ok" and row[3] is None
    finally:
        conn.close()


def test_fresh_install_is_current_with_new_columns(tmp_path):
    conn = db.connect(tmp_path / "fresh_v6.db")
    try:
        db.init_index_schema(conn)
        assert (
            _B9A_NEW_COLUMNS | _B13_NEW_COLUMN | _V4_NEW_COLUMN | _V6_NEW_COLUMN
        ) <= _columns(conn, "files")
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 6
        assert conn.execute(
            "SELECT dflt_value FROM pragma_table_info('files') "
            "WHERE name='no_gps_hidden'"
        ).fetchone()[0] == "0"
        # v5's new tables are part of a fresh install too.
        names = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"duplicates", "favorites"} <= names
    finally:
        conn.close()


# A v4-era ``files`` table (every current column) WITHOUT the v5 ``duplicates``/
# ``favorites`` tables. Used to prove the v4->v5 upgrade (new tables only, no
# ALTERs) on a pre-existing DB.
_V4_FILES_DDL = _V3_FILES_DDL.replace(
    "    stitch_status     TEXT,\n",
    "    stitch_status     TEXT,\n    stitch_projection TEXT,\n",
)


def _make_v4_index_db(path):
    """Create a synthetic v4 index DB (all files columns, no v5 tables) at version 4."""
    conn = db.connect(path)
    conn.executescript(_V4_FILES_DDL)
    conn.execute(
        "INSERT INTO files (dest_path, filename, media_type, sha256, status, lat, lon) "
        "VALUES ('C:/lib/y.jpg', 'y.jpg', 'photo', 'fed789', 'organized', 4.8, -75.6)"
    )
    conn.execute("INSERT INTO schema_version (version) VALUES (4)")
    conn.commit()
    return conn


def test_migrate_v4_to_v5_creates_new_tables(tmp_path):
    conn = _make_v4_index_db(tmp_path / "v4.db")
    try:
        # precondition: v4 has every files column but no v5 tables, at version 4
        names = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert not ({"duplicates", "favorites"} & names)
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 4

        db.init_index_schema(conn)  # creates the new tables + restamps

        names = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"duplicates", "favorites"} <= names
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 6
        # the pre-existing row survives untouched
        assert conn.execute("SELECT sha256 FROM files").fetchone()[0] == "fed789"
    finally:
        conn.close()


def test_migrate_v5_to_v6_adds_no_gps_hidden_losslessly(tmp_path):
    conn = db.connect(tmp_path / "v5.db")
    try:
        conn.executescript(_V4_FILES_DDL)
        conn.execute(
            "INSERT INTO files (dest_path, filename, media_type, sha256, status) "
            "VALUES ('C:/lib/_no-gps/q.jpg', 'q.jpg', 'photo', 'abc123', "
            "'quarantined')"
        )
        conn.execute("INSERT INTO schema_version (version) VALUES (5)")
        conn.commit()

        assert not (_V6_NEW_COLUMN & _columns(conn, "files"))
        db.init_index_schema(conn)

        assert _V6_NEW_COLUMN <= _columns(conn, "files")
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 6
        assert conn.execute(
            "SELECT filename, no_gps_hidden FROM files"
        ).fetchone() == ("q.jpg", 0)
        db.init_index_schema(conn)
        assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1
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
