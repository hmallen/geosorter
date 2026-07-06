"""Tests for the DJI SRT telemetry parser.

Fixtures under ``fixtures/srt/``:
* ``mini4pro_bracket.srt`` — REAL DJI Mini 4 Pro format (coordinates offset to a
  non-identifying value), cue 1 is a pre-lock ``(0,0)`` null-island frame.
* ``*_synthetic.srt`` — hand-authored to cover additional DJI format variants the
  user's two recent models do not emit. Clearly labelled non-real.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from geosorter.srt_parser import SrtResult, parse_srt, parse_srt_track

SRT_DIR = Path(__file__).parent / "fixtures" / "srt"


def test_bracket_real_first_fix():
    # Variant 1 (real): returns the first VALID fix, skipping the (0,0) pre-lock
    # frame in cue 1, and reports the matched variant + fix timestamp.
    res = parse_srt(SRT_DIR / "mini4pro_bracket.srt")
    assert res.gps_source == "srt"
    assert res.variant == "bracket"
    assert res.lat == pytest.approx(14.798240)
    assert res.lon == pytest.approx(-65.691450)
    assert res.frames_seen == 3
    assert res.first_fix_ts == "2024-08-26 18:06:42.493"


def test_phantom_paren_lonlat_order():
    # Variant 2 (synthetic): Phantom OSD GPS(lon,lat,alt) — longitude is FIRST.
    # The parser must not swap them.
    res = parse_srt(SRT_DIR / "phantom_paren_synthetic.srt")
    assert res.gps_source == "srt"
    assert res.variant == "paren"
    assert res.lat == pytest.approx(37.774900)
    assert res.lon == pytest.approx(-122.419400)


def test_mavic_bracket_spaced():
    # Variant 3 (synthetic): bracket family with spaces around the colon.
    res = parse_srt(SRT_DIR / "mavic_bracket_synthetic.srt")
    assert res.gps_source == "srt"
    assert res.variant == "bracket"
    assert res.lat == pytest.approx(51.500000)
    assert res.lon == pytest.approx(-0.120000)


def test_partial_flagged_when_never_locked():
    # GPS tokens present but every frame is null-island (0,0) -> srt_partial,
    # never silently-wrong coordinates.
    res = parse_srt(SRT_DIR / "partial_null_island_synthetic.srt")
    assert res.gps_source == "srt_partial"
    assert res.lat is None
    assert res.lon is None


def test_no_gps_tokens_returns_none():
    res = parse_srt(SRT_DIR / "no_gps_synthetic.srt")
    assert res.gps_source == "none"
    assert res.lat is None
    assert res.lon is None


def test_missing_file_returns_none():
    res = parse_srt(SRT_DIR / "does_not_exist.srt")
    assert isinstance(res, SrtResult)
    assert res.gps_source == "none"
    assert res.frames_seen == 0


def test_whitespace_only_separator_no_cross_frame_transpose(tmp_path):
    # A separator line carrying stray whitespace must still split frames, and
    # lat/lon must come from the SAME frame (the valid one), never pairing
    # frame 1's latitude with frame 2's longitude.
    text = (
        "1\n00:00:00,000 --> 00:00:00,033\n"
        '<font size="28">FrameCnt: 1\n2024-08-26 18:06:42.460\n'
        "[latitude: 0.000000] [longitude: 0.000000] </font>\n"
        " \n"  # whitespace-only separator between cues
        "2\n00:00:00,033 --> 00:00:00,066\n"
        '<font size="28">FrameCnt: 2\n2024-08-26 18:06:42.493\n'
        "[latitude: 14.798240] [longitude: -65.691450] </font>\n"
    )
    srt = tmp_path / "ws_sep.srt"
    srt.write_text(text, encoding="utf-8")
    res = parse_srt(srt)
    assert res.frames_seen == 2
    assert res.gps_source == "srt"
    assert res.lat == pytest.approx(14.798240)
    assert res.lon == pytest.approx(-65.691450)


# --- parse_srt_track (flight-track overlay) --------------------------------- #


def test_track_bracket_fixture_yields_ordered_fixes():
    track = parse_srt_track(SRT_DIR / "mini4pro_bracket.srt")
    # The first frame is a pre-lock (0,0) null-island fix and must be skipped.
    assert len(track) >= 2
    assert track[0] == pytest.approx((14.798240, -65.691450))
    for lat, lon in track:
        assert -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0
        assert (lat, lon) != (0.0, 0.0)


def test_track_paren_fixture_is_lon_first_safe():
    # The paren family writes GPS(lon,lat,alt): the track must come back (lat, lon).
    track = parse_srt_track(SRT_DIR / "phantom_paren_synthetic.srt")
    assert len(track) >= 2
    lat, lon = track[0]
    assert lat == pytest.approx(37.774900)
    assert lon == pytest.approx(-122.419400)


def test_track_no_gps_fixture_is_empty():
    assert parse_srt_track(SRT_DIR / "no_gps_synthetic.srt") == []


def test_track_missing_file_is_empty(tmp_path):
    assert parse_srt_track(tmp_path / "absent.SRT") == []
