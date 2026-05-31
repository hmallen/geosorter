"""Tests for the geosorter SQLite foundation (db module)."""

from geosorter import db


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
