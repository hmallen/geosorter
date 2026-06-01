"""Tests for the reverse geocoder (nearest populated place + geocode_cache)."""

from pathlib import Path

import pytest

from geosorter import db, geocoder, geonames_loader

FIXTURES = Path(__file__).parent / "fixtures" / "geonames"

# Fixture coordinates (cities500 test corpus).
BOULDER = (40.01499, -105.27055)  # geonameid 5574991
LONDON = (51.50853, -0.12574)  # geonameid 2643743


def _geonames_db(tmp_path, spatial_index="rtree"):
    gn_db = tmp_path / "geonames.db"
    geonames_loader.load(gn_db, FIXTURES, spatial_index=spatial_index)
    return db.connect(gn_db, integrity_check=False)


def test_nearest_city_boulder(tmp_path):
    conn = _geonames_db(tmp_path)
    try:
        result = geocoder.reverse_geocode(conn, *BOULDER)
    finally:
        conn.close()
    assert result.geonameid == 5574991
    assert result.ascii_name == "Boulder"
    assert result.place_string == "Boulder, Colorado, United States"
    assert result.feature_class == "P"
    assert result.geocode_confidence == "nearest_city"


def test_picks_nearest_when_bbox_has_several(tmp_path):
    # A point just south of Boulder still resolves to Boulder, not Denver,
    # even though both fall inside the 0.5 deg bbox.
    conn = _geonames_db(tmp_path)
    try:
        result = geocoder.reverse_geocode(conn, 40.0, -105.27)
    finally:
        conn.close()
    assert result.geonameid == 5574991


def test_columnar_fallback(tmp_path):
    conn = _geonames_db(tmp_path, spatial_index="columnar")
    try:
        result = geocoder.reverse_geocode(conn, *BOULDER)
    finally:
        conn.close()
    assert result.geonameid == 5574991
    assert result.place_string == "Boulder, Colorado, United States"


def test_cache_hit_single_row(tmp_path):
    conn = _geonames_db(tmp_path)
    idx = db.connect(tmp_path / "index.db", integrity_check=False)
    db.init_index_schema(idx)  # geocode_cache lives in the separate index DB
    try:
        first = geocoder.reverse_geocode(conn, *BOULDER, cache_conn=idx)
        second = geocoder.reverse_geocode(conn, *BOULDER, cache_conn=idx)
        count = idx.execute("SELECT COUNT(*) FROM geocode_cache").fetchone()[0]
    finally:
        conn.close()
        idx.close()
    assert count == 1
    assert second == first


def test_london_missing_admin2_left_join(tmp_path):
    conn = _geonames_db(tmp_path)
    try:
        result = geocoder.reverse_geocode(conn, *LONDON)
    finally:
        conn.close()
    assert result.geonameid == 2643743
    assert result.place_string.startswith("London")
    assert result.place_string.endswith("United Kingdom")


def test_no_candidates_returns_fallback(tmp_path):
    conn = _geonames_db(tmp_path)
    try:
        result = geocoder.reverse_geocode(conn, 10.0, 10.0)  # ocean, far from fixtures
    finally:
        conn.close()
    assert result.geonameid is None
    assert result.geocode_confidence == "fallback"


def test_none_coords_raises(tmp_path):
    conn = _geonames_db(tmp_path)
    try:
        with pytest.raises(ValueError):
            geocoder.reverse_geocode(conn, None, None)
    finally:
        conn.close()


def _insert_city(conn, geonameid, lat, lon, name="HighLat"):
    conn.execute(
        "INSERT INTO geonames(geonameid, name, ascii_name, lat, lon, feature_class, "
        "feature_code, country_code, admin1_code, admin2_code, population, timezone) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (geonameid, name, name, lat, lon, "P", "PPL", "NO", "", None, 1000, "Europe/Oslo"),
    )
    conn.execute(
        "INSERT INTO geonames_rtree(id, min_lat, max_lat, min_lon, max_lon) VALUES (?,?,?,?,?)",
        (geonameid, lat, lat, lon, lon),
    )
    conn.commit()


def _insert_feature(conn, geonameid, lat, lon, name, feature_class="L", feature_code="PRK"):
    conn.execute(
        "INSERT INTO geonames(geonameid, name, ascii_name, lat, lon, feature_class, "
        "feature_code, country_code, admin1_code, admin2_code, population, timezone) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (geonameid, name, name, lat, lon, feature_class, feature_code,
         "US", "CO", None, 0, "America/Denver"),
    )
    conn.execute(
        "INSERT INTO geonames_rtree(id, min_lat, max_lat, min_lon, max_lon) VALUES (?,?,?,?,?)",
        (geonameid, lat, lat, lon, lon),
    )
    conn.commit()


# Query point and offsets (~111 km / deg of latitude at 40 N): 0.009 deg ~ 1 km,
# 0.018 deg ~ 2 km, 0.072 deg ~ 8 km. Used to place a city and a feature at known
# distances from QUERY for the prefer-nearest-feature heuristic.
QUERY = (40.0, -105.0)


def test_feature_within_radius_beats_nearer_city(tmp_path):
    # A park ~2 km away wins over a town ~1 km away because it is within the 5 km
    # feature_proximity_km, even though the town is marginally closer.
    conn = _geonames_db(tmp_path)
    _insert_city(conn, 9000001, 40.009, -105.0, name="NearTown")
    _insert_feature(conn, 9000002, 40.018, -105.0, "Rocky Mountain NP")
    try:
        result = geocoder.reverse_geocode(conn, *QUERY, feature_proximity_km=5.0)
    finally:
        conn.close()
    assert result.geonameid == 9000002
    assert result.ascii_name == "Rocky Mountain NP"
    assert result.feature_class == "L"
    assert result.geocode_confidence == "nearest_feature"


def test_feature_beyond_radius_yields_city(tmp_path):
    # The same town, but the park is now ~8 km away — beyond 5 km — so the town wins.
    conn = _geonames_db(tmp_path)
    _insert_city(conn, 9000001, 40.009, -105.0, name="NearTown")
    _insert_feature(conn, 9000002, 40.072, -105.0, "Far Park")
    try:
        result = geocoder.reverse_geocode(conn, *QUERY, feature_proximity_km=5.0)
    finally:
        conn.close()
    assert result.geonameid == 9000001
    assert result.feature_class == "P"
    assert result.geocode_confidence == "nearest_city"


def test_feature_only_within_radius(tmp_path):
    # A peak in range with no nearby town still resolves to the feature.
    conn = _geonames_db(tmp_path)
    _insert_feature(conn, 9000003, 40.018, -105.0, "Longs Peak",
                    feature_class="T", feature_code="PK")
    try:
        result = geocoder.reverse_geocode(conn, *QUERY, feature_proximity_km=5.0)
    finally:
        conn.close()
    assert result.geonameid == 9000003
    assert result.feature_class == "T"
    assert result.geocode_confidence == "nearest_feature"


def test_candidates_lists_city_and_feature(tmp_path):
    # The candidates() helper (powering geocode-test) surfaces both the town and
    # the feature with distances, nearest first.
    conn = _geonames_db(tmp_path)
    _insert_city(conn, 9000001, 40.009, -105.0, name="NearTown")
    _insert_feature(conn, 9000002, 40.018, -105.0, "Rocky Mountain NP")
    try:
        cands = geocoder.candidates(conn, *QUERY)
    finally:
        conn.close()
    ids = [c.geonameid for c in cands]
    assert 9000001 in ids and 9000002 in ids
    # Sorted nearest-first by distance.
    dists = [c.dist_km for c in cands]
    assert dists == sorted(dists)


def test_high_latitude_longitude_correction(tmp_path):
    # At 70 deg N, 1 deg of longitude (~38 km) is well within reach of a drone,
    # but a fixed +/-0.5 deg lon bbox would exclude it. The cos(lat)-corrected
    # bbox must still find the city instead of returning a spurious fallback.
    conn = _geonames_db(tmp_path)
    _insert_city(conn, 9999001, 70.0, 20.0)
    try:
        result = geocoder.reverse_geocode(conn, 70.0, 21.0)  # 1.0 deg lon east
    finally:
        conn.close()
    assert result.geonameid == 9999001
    assert result.geocode_confidence == "nearest_city"
