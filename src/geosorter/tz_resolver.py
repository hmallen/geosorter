"""GPS-derived local capture time (offline ``timezonefinder`` + ``zoneinfo``).

The capture timestamp from :mod:`geosorter.metadata` is a **naive** wall-clock
string with no offset. Its meaning depends on the source tag:

* ``QuickTime:CreateDate`` (DJI video) is **UTC** — interpret as UTC and convert
  to the GPS-derived IANA zone.
* ``EXIF:DateTimeOriginal`` / ``EXIF:CreateDate`` (DJI photo) is the camera's
  **local** wall-clock — attach the GPS-derived zone without shifting the clock.

Either way the IANA zone comes from the GPS coordinate, so date folders are
correct across timezones and midnight boundaries. A wall-clock that falls in a
DST fall-back overlap (the same local time occurs twice) is flagged
``tz_ambiguous``.

``timezonefinder`` is instantiated once (the constructor is expensive) and reused
via the module-level ``_FINDER``. ``tzdata`` backs ``zoneinfo`` on Windows, which
has no system IANA database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from timezonefinder import TimezoneFinder

# One reused instance — lazily created so import stays cheap and tests can patch.
_FINDER: TimezoneFinder | None = None

# Naive timestamp shapes B3 may receive (EXIF colon-date, SRT dotted-ms).
_FORMATS = ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class LocalTime:
    """GPS-derived local capture time and its provenance flags."""

    iana_zone: str | None
    capture_ts_utc: str | None  # ISO 8601, UTC
    capture_ts_local: str | None  # ISO 8601, with local offset
    local_date: str | None  # 'YYYY-MM-DD' (drives the date folder)
    local_time_hms: str | None  # 'HH-MM-SS' (drives the filename)
    tz_ambiguous: bool


_EMPTY = LocalTime(None, None, None, None, None, False)


def _finder() -> TimezoneFinder:
    global _FINDER
    if _FINDER is None:
        _FINDER = TimezoneFinder()
    return _FINDER


def _parse_naive(raw: str) -> datetime | None:
    for fmt in _FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def resolve_local_time(
    lat: float | None,
    lon: float | None,
    capture_ts_raw: str | None,
    capture_ts_source_tag: str | None,
) -> LocalTime:
    """Resolve a naive capture timestamp to GPS-derived local time.

    Returns :data:`LocalTime`. All datetime fields are ``None`` when the
    coordinate has no resolvable zone or the timestamp is missing/unparseable
    (such files route to quarantine upstream in B4).
    """
    if lat is None or lon is None or not capture_ts_raw:
        return _EMPTY

    zone_name = _finder().timezone_at(lng=lon, lat=lat)
    if zone_name is None:
        return _EMPTY

    naive = _parse_naive(capture_ts_raw)
    if naive is None:
        return LocalTime(zone_name, None, None, None, None, False)

    zone = ZoneInfo(zone_name)
    ambiguous = False

    if capture_ts_source_tag and capture_ts_source_tag.startswith("QuickTime"):
        # UTC source -> convert to the local zone (unambiguous).
        dt_utc = naive.replace(tzinfo=timezone.utc)
        dt_local = dt_utc.astimezone(zone)
    else:
        # EXIF local wall-clock -> attach the zone, do not shift.
        dt_local = naive.replace(tzinfo=zone)
        dt_utc = dt_local.astimezone(timezone.utc)
        # DST fall-back overlap: the same wall-clock maps to two UTC offsets.
        if dt_local.utcoffset() != naive.replace(tzinfo=zone, fold=1).utcoffset():
            ambiguous = True

    return LocalTime(
        iana_zone=zone_name,
        capture_ts_utc=dt_utc.isoformat(),
        capture_ts_local=dt_local.isoformat(),
        local_date=dt_local.strftime("%Y-%m-%d"),
        local_time_hms=dt_local.strftime("%H-%M-%S"),
        tz_ambiguous=ambiguous,
    )


def local_time_from_utc(
    lat: float | None, lon: float | None, capture_ts_utc: str | None
) -> LocalTime:
    """Recompute :class:`LocalTime` for a stored UTC instant at a new coordinate.

    Used by the B8 manual re-tag: an already-organized file carries a resolved
    ``capture_ts_utc`` (ISO 8601, UTC); when the user assigns a new location the
    local date/time must be recomputed against the new coordinate's IANA zone. The
    instant is unambiguous (already UTC), so there is no DST-fold case. Returns
    :data:`_EMPTY` when the coordinate has no resolvable zone or ``capture_ts_utc``
    is missing/unparseable.
    """
    if lat is None or lon is None or not capture_ts_utc:
        return _EMPTY

    zone_name = _finder().timezone_at(lng=lon, lat=lat)
    if zone_name is None:
        return _EMPTY

    try:
        dt_utc = datetime.fromisoformat(capture_ts_utc)
    except ValueError:
        return LocalTime(zone_name, None, None, None, None, False)
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)

    dt_local = dt_utc.astimezone(ZoneInfo(zone_name))
    return LocalTime(
        iana_zone=zone_name,
        capture_ts_utc=dt_utc.astimezone(timezone.utc).isoformat(),
        capture_ts_local=dt_local.isoformat(),
        local_date=dt_local.strftime("%Y-%m-%d"),
        local_time_hms=dt_local.strftime("%H-%M-%S"),
        tz_ambiguous=False,
    )
