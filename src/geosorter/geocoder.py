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

The prefer-nearest-feature heuristic (parks/peaks/hydro, feature classes L/T/H) is
Phase 0b / task B5 — not implemented here.
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
    """Return ``(geonameid, ascii_name, lat, lon)`` rows within the bbox.

    Cities only (``feature_class='P'``). Uses the R-tree when present, otherwise
    the covering ``(lat, lon)`` index via a ``BETWEEN`` range scan.

    The longitude half-width is widened by ``1/cos(lat)`` so the bounding box
    stays roughly square in real distance — a degree of longitude shrinks toward
    the poles, and a fixed degree window would otherwise exclude the true-nearest
    city at high latitudes.
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
            f"SELECT geonameid, ascii_name, lat, lon FROM geonames "
            f"WHERE geonameid IN ({placeholders}) AND feature_class='P'",
            ids,
        ).fetchall()
    return conn.execute(
        "SELECT geonameid, ascii_name, lat, lon FROM geonames "
        "WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? AND feature_class='P'",
        (lo_lat, hi_lat, lo_lon, hi_lon),
    ).fetchall()


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


def reverse_geocode(
    geonames_conn: sqlite3.Connection,
    lat: float | None,
    lon: float | None,
    *,
    cache_conn: sqlite3.Connection | None = None,
    bbox_deg: float = 0.5,
    round_dp: int = 4,
) -> GeocodeResult:
    """Reverse-geocode ``(lat, lon)`` to the nearest populated place.

    ``geonames_conn`` is the GeoNames reference DB; ``cache_conn`` (the index DB)
    is consulted and updated only when provided. Coordinates are rounded to
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

    candidates = _candidates(geonames_conn, lat, lon, bbox_deg)
    if not candidates:
        result = GeocodeResult(None, None, None, None, "fallback")
    else:
        best = min(candidates, key=lambda c: _haversine(lat, lon, c[2], c[3]))
        ascii_name, place_string, feature_class = _resolve_place(geonames_conn, best[0])
        result = GeocodeResult(best[0], ascii_name, place_string, feature_class, "nearest_city")

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
