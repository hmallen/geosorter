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

from geosorter.srt_parser import (
    SrtResult,
    parse_srt,
    parse_srt_track,
    parse_srt_track_samples,
)

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
    # Variant 2 (synthetic): Phantom OSD GPS(lon,lat,...) — longitude is FIRST.
    # The parser must not swap them. (The third field is not read; this family's
    # altitude comes from BAROMETER — see the altitude tests below.)
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
    # The paren family writes GPS(lon,lat,...): the track must come back (lat, lon).
    track = parse_srt_track(SRT_DIR / "phantom_paren_synthetic.srt")
    assert len(track) >= 2
    lat, lon = track[0]
    assert lat == pytest.approx(37.774900)
    assert lon == pytest.approx(-122.419400)


def test_track_no_gps_fixture_is_empty():
    assert parse_srt_track(SRT_DIR / "no_gps_synthetic.srt") == []


def test_track_missing_file_is_empty(tmp_path):
    assert parse_srt_track(tmp_path / "absent.SRT") == []


def test_timed_track_uses_cue_start_and_skips_prelock():
    samples = parse_srt_track_samples(SRT_DIR / "mini4pro_bracket.srt")
    assert len(samples) == 2
    assert samples[0].time_s == pytest.approx(0.033)
    assert samples[0].lat == pytest.approx(14.798240)
    assert samples[0].lon == pytest.approx(-65.691450)
    assert samples[-1].time_s == pytest.approx(0.066)


def test_timed_track_accepts_dot_milliseconds(tmp_path):
    srt = tmp_path / "dot.srt"
    srt.write_text(
        "1\n00:00:01.250 --> 00:00:01.283\n"
        "[latitude: 40.000000] [longitude: -105.000000]\n",
        encoding="utf-8",
    )
    samples = parse_srt_track_samples(srt)
    assert len(samples) == 1
    assert samples[0].time_s == pytest.approx(1.25)


def test_timed_track_skips_malformed_timing_but_static_track_keeps_fix(tmp_path):
    srt = tmp_path / "bad-time.srt"
    srt.write_text(
        "1\nnot a timing line\n"
        "[latitude: 40.000000] [longitude: -105.000000]\n\n"
        "2\n00:00:02,000 --> 00:00:02,033\n"
        "[latitude: 40.100000] [longitude: -105.100000]\n",
        encoding="utf-8",
    )
    assert len(parse_srt_track(srt)) == 2
    samples = parse_srt_track_samples(srt)
    assert [(s.time_s, s.lat, s.lon) for s in samples] == [
        (2.0, 40.1, -105.1),
    ]


def test_timed_track_prefers_relative_altitude_over_absolute():
    # The real bracket payload carries `[rel_alt: X abs_alt: Y]`: height above
    # takeoff wins, and the datum is reported so a consumer can label it.
    samples = parse_srt_track_samples(SRT_DIR / "mini4pro_bracket.srt")
    assert [s.alt for s in samples] == [pytest.approx(0.0), pytest.approx(0.0)]
    assert {s.alt_ref for s in samples} == {"relative"}


def test_timed_track_reads_spaced_bracket_altitude():
    # Older bracket payloads write a single barometric `[altitude : X]` (MSL).
    samples = parse_srt_track_samples(SRT_DIR / "mavic_bracket_synthetic.srt")
    assert samples[0].alt == pytest.approx(35.0)
    assert samples[0].alt_ref == "absolute"
    assert samples[1].alt == pytest.approx(35.1)


def test_timed_track_reads_paren_barometer_not_satellite_count():
    # The paren family's height is BAROMETER. The third GPS() field (16 here) is
    # the satellite count on DJI GO OSD builds and must never surface as metres.
    samples = parse_srt_track_samples(SRT_DIR / "phantom_paren_synthetic.srt")
    assert samples[0].alt == pytest.approx(8.50)
    assert samples[0].alt_ref == "relative"
    assert samples[1].alt == pytest.approx(8.51)


def test_timed_track_keeps_fix_when_altitude_absent(tmp_path):
    # GPS gates the sample; a cue with no altitude token still contributes its
    # position, with alt left None rather than a fabricated 0.
    srt = tmp_path / "no-alt.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:00,033\n"
        "[latitude: 40.000000] [longitude: -105.000000]\n",
        encoding="utf-8",
    )
    samples = parse_srt_track_samples(srt)
    assert len(samples) == 1
    assert samples[0].alt is None
    assert samples[0].alt_ref is None


def test_timed_track_reads_negative_and_integer_altitudes(tmp_path):
    # Below-takeoff flights are real (canyon/downhill launches) and DJI does not
    # always write a fractional part.
    srt = tmp_path / "neg-alt.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:00,033\n"
        "[latitude: 40.000000] [longitude: -105.000000] [rel_alt: -12 abs_alt: 1400]\n",
        encoding="utf-8",
    )
    samples = parse_srt_track_samples(srt)
    assert samples[0].alt == pytest.approx(-12.0)
    assert samples[0].alt_ref == "relative"


def test_timed_track_skips_backward_timestamps_but_keeps_equal_times(tmp_path):
    srt = tmp_path / "order.srt"
    srt.write_text(
        "1\n00:00:02,000 --> 00:00:02,033\n"
        "[latitude: 40.000000] [longitude: -105.000000]\n\n"
        "2\n00:00:01,000 --> 00:00:01,033\n"
        "[latitude: 40.100000] [longitude: -105.100000]\n\n"
        "3\n00:00:02,000 --> 00:00:02,033\n"
        "[latitude: 40.200000] [longitude: -105.200000]\n",
        encoding="utf-8",
    )
    samples = parse_srt_track_samples(srt)
    assert [s.time_s for s in samples] == [2.0, 2.0]
