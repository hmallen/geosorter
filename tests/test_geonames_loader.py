"""Tests for the GeoNames bootstrap loader (parsing + loading into SQLite)."""

from pathlib import Path

import pytest

from geosorter import db, geonames_loader

FIXTURES = Path(__file__).parent / "fixtures" / "geonames"


def _counts(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_load_populates_all_tables(tmp_path):
    gn_db = tmp_path / "geonames.db"
    counts = geonames_loader.load(gn_db, FIXTURES, spatial_index="rtree")

    assert counts["geonames"] == 3
    assert counts["admin1"] == 2
    assert counts["admin2"] == 2
    assert counts["countries"] == 2

    conn = db.connect(gn_db, integrity_check=False)
    try:
        assert _counts(conn, "geonames") == 3
        assert _counts(conn, "admin1_codes") == 2
        assert _counts(conn, "admin2_codes") == 2
        assert _counts(conn, "country_info") == 2
        # rtree populated 1:1 with geonames
        assert _counts(conn, "geonames_rtree") == 3
    finally:
        conn.close()


def test_load_parses_city_fields(tmp_path):
    gn_db = tmp_path / "geonames.db"
    geonames_loader.load(gn_db, FIXTURES, spatial_index="rtree")
    conn = db.connect(gn_db, integrity_check=False)
    try:
        row = conn.execute(
            "SELECT name, ascii_name, lat, lon, feature_class, country_code, "
            "admin1_code, admin2_code, population, timezone "
            "FROM geonames WHERE geonameid = 5574991"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "Boulder"
    assert row[1] == "Boulder"
    assert row[2] == pytest.approx(40.01499)
    assert row[3] == pytest.approx(-105.27055)
    assert row[4] == "P"
    assert row[5] == "US"
    assert row[6] == "CO"
    assert row[7] == "013"
    assert row[8] == 105673
    assert row[9] == "America/Denver"


def test_admin_and_country_join_resolves_place(tmp_path):
    gn_db = tmp_path / "geonames.db"
    geonames_loader.load(gn_db, FIXTURES, spatial_index="rtree")
    conn = db.connect(gn_db, integrity_check=False)
    try:
        # Resolve "Boulder" -> region (admin1) + country names via joins.
        row = conn.execute(
            """
            SELECT g.name, a1.name AS region, c.country_name
            FROM geonames g
            JOIN admin1_codes a1 ON a1.code = g.country_code || '.' || g.admin1_code
            JOIN country_info c ON c.country_code = g.country_code
            WHERE g.geonameid = 5574991
            """
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "Boulder"
    assert row[1] == "Colorado"
    assert row[2] == "United States"


def test_load_is_idempotent(tmp_path):
    gn_db = tmp_path / "geonames.db"
    geonames_loader.load(gn_db, FIXTURES, spatial_index="rtree")
    geonames_loader.load(gn_db, FIXTURES, spatial_index="rtree")  # re-run
    conn = db.connect(gn_db, integrity_check=False)
    try:
        assert _counts(conn, "geonames") == 3  # no duplicates
        assert _counts(conn, "geonames_rtree") == 3
    finally:
        conn.close()


def test_load_columnar_fallback_no_rtree(tmp_path):
    gn_db = tmp_path / "geonames.db"
    counts = geonames_loader.load(gn_db, FIXTURES, spatial_index="columnar")
    assert counts["geonames"] == 3
    conn = db.connect(gn_db, integrity_check=False)
    try:
        tnames = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "geonames_rtree" not in tnames
    finally:
        conn.close()
