"""Offline reverse geocoding: nearest populated place over the GeoNames DB.

Phase 0a is **cities-only** — every ``geonames`` row is ``feature_class='P'``.
A coordinate is resolved to the nearest populated place via a 0.5 deg bounding-box
pre-filter (R-tree when available, else the columnar ``(lat, lon)`` index) followed
by an exact Haversine ranking of the handful of candidates. Results are cached in
the index DB's ``geocode_cache`` keyed on rounded coordinates.

The ``geonames`` reference data and the ``geocode_cache`` live in **separate
databases** (decision D24), so the geonames connection and the cache connection
are passed independently. ``geonameid`` is the canonical key; ``place_string`` is
display-only (User Note — prevents library bifurcation on GeoNames updates).

**Prefer-nearest-feature heuristic (Phase 0b / B5).** When the geonames DB has been
bootstrapped with ``--features`` it also holds L (parks/areas), T (terrain/peaks),
and H (hydro) rows. A coordinate then resolves by candidate priority:

1. the nearest L/T/H feature, if it is within ``feature_proximity_km`` → ``nearest_feature``
2. else the nearest populated place (class ``P``) → ``nearest_city``
3. else the nearest feature even beyond the radius (no city in range) → ``nearest_feature``
4. else → ``fallback``

GeoNames features are point centroids, not polygons, so "inside a park" is an
approximation — a capture near a large park's edge can fall outside the radius and
resolve to a town instead (documented failure mode).
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

# Place-resolution join. LEFT JOIN because some places (e.g. capitals) lack an
# admin1/admin2 code — an inner join would drop the row and break geocoding.
_PLACE_SQL = """
SELECT g.geonameid, g.ascii_name, g.feature_class, g.country_code,
       a1.name AS region, c.country_name
FROM geonames g
LEFT JOIN admin1_codes a1 ON a1.code = g.country_code || '.' || g.admin1_code
LEFT JOIN country_info  c ON c.country_code = g.country_code
WHERE g.geonameid = ?
"""


@dataclass(frozen=True)
class GeocodeResult:
    """Reverse-geocode outcome for one coordinate.

    ``geonameid`` is the canonical stable key; ``ascii_name`` is the GeoNames
    filesystem-safe city name (the folder source); ``place_string`` is the
    ``"City, Region, Country"`` display string. ``geocode_confidence`` is
    ``'nearest_city'`` when a place was found, ``'fallback'`` when none was.
    """

    geonameid: int | None
    ascii_name: str | None
    place_string: str | None
    feature_class: str | None
    geocode_confidence: str


@dataclass(frozen=True)
class Candidate:
    """One nearby GeoNames place considered during reverse geocoding.

    Surfaced by :func:`candidates` to power the ``geocode-test`` CLI verb so the
    heuristic's inputs are inspectable. ``dist_km`` is the great-circle distance
    from the query coordinate.
    """

    geonameid: int
    ascii_name: str | None
    feature_class: str | None
    dist_km: float


_FEATURE_CLASSES = ("L", "T", "H")


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _has_rtree(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='geonames_rtree'"
    ).fetchone()
    return row is not None


def _candidates(
    conn: sqlite3.Connection, lat: float, lon: float, bbox_deg: float
) -> list[tuple]:
    """Return ``(geonameid, ascii_name, lat, lon, feature_class)`` rows in the bbox.

    All classes (cities ``P`` plus features ``L``/``T``/``H``) — the heuristic
    splits them downstream. Uses the R-tree when present, otherwise the covering
    ``(lat, lon)`` index via a ``BETWEEN`` range scan.

    The longitude half-width is widened by ``1/cos(lat)`` so the bounding box
    stays roughly square in real distance — a degree of longitude shrinks toward
    the poles, and a fixed degree window would otherwise exclude the true-nearest
    place at high latitudes.
    """
    lon_deg = bbox_deg / max(math.cos(math.radians(lat)), 0.01)
    lo_lat, hi_lat = lat - bbox_deg, lat + bbox_deg
    lo_lon, hi_lon = lon - lon_deg, lon + lon_deg
    if _has_rtree(conn):
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM geonames_rtree "
                "WHERE min_lat>=? AND max_lat<=? AND min_lon>=? AND max_lon<=?",
                (lo_lat, hi_lat, lo_lon, hi_lon),
            )
        ]
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        return conn.execute(
            f"SELECT geonameid, ascii_name, lat, lon, feature_class FROM geonames "
            f"WHERE geonameid IN ({placeholders})",
            ids,
        ).fetchall()
    return conn.execute(
        "SELECT geonameid, ascii_name, lat, lon, feature_class FROM geonames "
        "WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
        (lo_lat, hi_lat, lo_lon, hi_lon),
    ).fetchall()


def candidates(
    geonames_conn: sqlite3.Connection,
    lat: float,
    lon: float,
    *,
    bbox_deg: float = 0.5,
) -> list[Candidate]:
    """Return nearby places as :class:`Candidate` rows, nearest first.

    A thin inspection helper over :func:`_candidates` + :func:`_haversine` for the
    ``geocode-test`` verb; does not touch the cache or pick a winner.
    """
    rows = _candidates(geonames_conn, lat, lon, bbox_deg)
    out = [
        Candidate(r[0], r[1], r[4], _haversine(lat, lon, r[2], r[3]))
        for r in rows
    ]
    out.sort(key=lambda c: c.dist_km)
    return out


def _resolve_place(conn: sqlite3.Connection, geonameid: int) -> tuple[str | None, str | None, str | None]:
    """Return ``(ascii_name, place_string, feature_class)`` for a geonameid."""
    row = conn.execute(_PLACE_SQL, (geonameid,)).fetchone()
    if row is None:
        return None, None, None
    ascii_name, feature_class = row[1], row[2]
    region, country = row[4], row[5]
    parts = [p for p in (ascii_name, region, country) if p]
    place_string = ", ".join(parts) if parts else None
    return ascii_name, place_string, feature_class


def _choose(rows: list[tuple], lat: float, lon: float, feature_proximity_km: float):
    """Pick the winning ``(geonameid, confidence)`` from bbox candidate rows.

    Priority (see module docstring): nearest feature within the radius → nearest
    city → nearest feature beyond the radius → nothing. Each ``row`` is a
    ``(geonameid, ascii_name, lat, lon, feature_class)`` tuple from :func:`_candidates`.
    """
    nearest_city = None  # (dist, geonameid)
    nearest_feat = None
    for gid, _name, rlat, rlon, fclass in rows:
        dist = _haversine(lat, lon, rlat, rlon)
        if fclass == "P":
            if nearest_city is None or dist < nearest_city[0]:
                nearest_city = (dist, gid)
        elif fclass in _FEATURE_CLASSES:
            if nearest_feat is None or dist < nearest_feat[0]:
                nearest_feat = (dist, gid)

    if nearest_feat is not None and nearest_feat[0] <= feature_proximity_km:
        return nearest_feat[1], "nearest_feature"
    if nearest_city is not None:
        return nearest_city[1], "nearest_city"
    if nearest_feat is not None:
        return nearest_feat[1], "nearest_feature"
    return None, "fallback"


def reverse_geocode(
    geonames_conn: sqlite3.Connection,
    lat: float | None,
    lon: float | None,
    *,
    cache_conn: sqlite3.Connection | None = None,
    bbox_deg: float = 0.5,
    round_dp: int = 4,
    feature_proximity_km: float = 5.0,
) -> GeocodeResult:
    """Reverse-geocode ``(lat, lon)`` to the nearest place (feature-preferring).

    ``geonames_conn`` is the GeoNames reference DB; ``cache_conn`` (the index DB)
    is consulted and updated only when provided. A named park/peak/hydro feature
    within ``feature_proximity_km`` wins over the nearest populated place (see the
    module docstring for the full priority order). Coordinates are rounded to
    ``round_dp`` decimals (~11 m at 4 dp) for the cache key. Raises
    :class:`ValueError` if either coordinate is ``None`` (no-GPS files route to
    quarantine upstream).
    """
    if lat is None or lon is None:
        raise ValueError("reverse_geocode requires non-None coordinates")

    lat_key, lon_key = round(lat, round_dp), round(lon, round_dp)

    if cache_conn is not None:
        hit = cache_conn.execute(
            "SELECT geonameid, place_string, feature_class, geocode_confidence "
            "FROM geocode_cache WHERE lat_key=? AND lon_key=?",
            (lat_key, lon_key),
        ).fetchone()
        if hit is not None:
            gid, place_string, feature_class, confidence = hit
            ascii_name = None
            if gid is not None:
                row = geonames_conn.execute(
                    "SELECT ascii_name FROM geonames WHERE geonameid=?", (gid,)
                ).fetchone()
                ascii_name = row[0] if row else None
            return GeocodeResult(gid, ascii_name, place_string, feature_class, confidence)

    rows = _candidates(geonames_conn, lat, lon, bbox_deg)
    gid, confidence = _choose(rows, lat, lon, feature_proximity_km)
    if gid is None:
        result = GeocodeResult(None, None, None, None, "fallback")
    else:
        ascii_name, place_string, feature_class = _resolve_place(geonames_conn, gid)
        result = GeocodeResult(gid, ascii_name, place_string, feature_class, confidence)

    if cache_conn is not None:
        cache_conn.execute(
            "INSERT OR REPLACE INTO geocode_cache"
            "(lat_key, lon_key, geonameid, place_string, feature_class, geocode_confidence) "
            "VALUES (?,?,?,?,?,?)",
            (lat_key, lon_key, result.geonameid, result.place_string,
             result.feature_class, result.geocode_confidence),
        )
        cache_conn.commit()

    return result
