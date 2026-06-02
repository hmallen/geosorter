"""Tests for GPS-derived local-time resolution (timezonefinder + zoneinfo)."""

from geosorter import tz_resolver

# America/Denver (UTC-7 standard, UTC-6 DST).
DENVER = (39.73915, -104.9847)


def test_video_utc_boundary_crosses_date():
    # A QuickTime CreateDate is UTC. 02:30 UTC on Jan 1 is 19:30 the previous
    # day in Denver (MST, UTC-7) -> the file belongs to the 2023-12-31 folder.
    result = tz_resolver.resolve_local_time(
        *DENVER, "2024:01:01 02:30:00", "QuickTime:CreateDate"
    )
    assert result.iana_zone == "America/Denver"
    assert result.local_date == "2023-12-31"
    assert result.local_time_hms == "19-30-00"
    assert result.tz_ambiguous is False
    assert result.capture_ts_utc.startswith("2024-01-01T02:30:00")


def test_photo_exif_local_is_not_shifted():
    # EXIF DateTimeOriginal is already the camera's local wall-clock; the GPS
    # zone is attached but the wall-clock value is preserved as-is.
    result = tz_resolver.resolve_local_time(
        *DENVER, "2024:07:04 09:15:00", "EXIF:DateTimeOriginal"
    )
    assert result.iana_zone == "America/Denver"
    assert result.local_date == "2024-07-04"
    assert result.local_time_hms == "09-15-00"
    assert result.tz_ambiguous is False


def test_dst_fall_back_fold_is_ambiguous():
    # 01:30 on 2024-11-03 occurs twice in America/Denver (DST fall-back).
    result = tz_resolver.resolve_local_time(
        *DENVER, "2024:11:03 01:30:00", "EXIF:DateTimeOriginal"
    )
    assert result.tz_ambiguous is True


def test_ocean_resolves_to_etc_gmt_zone():
    # timezonefinder 8.x covers oceans (Etc/GMT zones); a result is still usable.
    result = tz_resolver.resolve_local_time(
        0.0, -30.0, "2024:06:01 12:00:00", "QuickTime:CreateDate"
    )
    assert result.iana_zone is not None
    assert result.local_date is not None
    assert result.local_time_hms is not None


def test_none_zone_returns_empty(monkeypatch):
    # Defensive: if the finder yields no zone, all datetime fields are None.
    class _NoZone:
        def timezone_at(self, **kwargs):
            return None

    monkeypatch.setattr(tz_resolver, "_FINDER", _NoZone())
    result = tz_resolver.resolve_local_time(
        *DENVER, "2024:07:04 09:15:00", "EXIF:DateTimeOriginal"
    )
    assert result.iana_zone is None
    assert result.local_date is None
    assert result.local_time_hms is None
    assert result.tz_ambiguous is False


def test_missing_timestamp_returns_empty():
    result = tz_resolver.resolve_local_time(*DENVER, None, None)
    assert result.local_date is None
    assert result.capture_ts_utc is None


# --- local_time_from_utc (B8 re-tag): recompute local time from a stored UTC ---


def test_local_time_from_utc_resolves_denver():
    # 16:15 UTC on 2024-07-04 is 10:15 MDT (UTC-6 summer) in Denver.
    result = tz_resolver.local_time_from_utc(*DENVER, "2024-07-04T16:15:00+00:00")
    assert result.iana_zone == "America/Denver"
    assert result.local_date == "2024-07-04"
    assert result.local_time_hms == "10-15-00"
    assert result.capture_ts_utc.startswith("2024-07-04T16:15:00")
    assert result.tz_ambiguous is False


def test_local_time_from_utc_differs_across_timezone():
    # The same instant resolves to a later wall-clock (and possibly next day) in Tokyo.
    denver = tz_resolver.local_time_from_utc(*DENVER, "2024-07-04T16:15:00+00:00")
    tokyo = tz_resolver.local_time_from_utc(35.68, 139.76, "2024-07-04T16:15:00+00:00")
    assert tokyo.iana_zone == "Asia/Tokyo"
    # 16:15 UTC = 01:15 next day JST (UTC+9): a different date than Denver's.
    assert tokyo.local_date == "2024-07-05"
    assert tokyo.local_date != denver.local_date


def test_local_time_from_utc_unparseable_returns_empty():
    assert tz_resolver.local_time_from_utc(*DENVER, "not-a-timestamp").local_date is None
    assert tz_resolver.local_time_from_utc(*DENVER, None).local_date is None
