"""Tests for the Windows-safe sanitizer and destination-path computation."""

from pathlib import Path

import pytest

from geosorter import pathing
from geosorter.geocoder import GeocodeResult
from geosorter.tz_resolver import LocalTime


# --------------------------------------------------------------------------- #
# sanitize_component — edge-case corpus
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ('A<b>:c', "Abc"),            # illegal chars stripped
        ('a/b\\c|d?e*f"g', "abcdefg"),  # all illegal chars stripped
        ("name. ", "name"),           # trailing dot + space
        ("  spaced  ", "spaced"),      # surrounding whitespace
        ("Café", "Café"),  # NFC normalization (e + acute -> é)
        ("CON", "_CON"),              # reserved device name
        ("com1", "_com1"),            # reserved, case-insensitive, case preserved
        ("LPT9", "_LPT9"),            # reserved
        ("NUL.JPG", "_NUL.JPG"),      # reserved stem with extension
        ("Boulder", "Boulder"),       # ordinary name untouched
    ],
)
def test_sanitize_component_corpus(raw, expected):
    assert pathing.sanitize_component(raw) == expected


def test_sanitize_truncates_to_max_len():
    out = pathing.sanitize_component("x" * 50, max_len=40)
    assert out == "x" * 40


def test_sanitize_empty_uses_fallback():
    assert pathing.sanitize_component("***", fallback="5574991") == "5574991"
    assert pathing.sanitize_component(None, fallback="5574991") == "5574991"


def test_sanitize_truncation_drops_trailing_space():
    # Truncation must not leave a trailing space that Windows would reject.
    out = pathing.sanitize_component("abcdefghij " + "z" * 40, max_len=11)
    assert out == "abcdefghij"


# --------------------------------------------------------------------------- #
# compute_dest_path
# --------------------------------------------------------------------------- #
def _local():
    return LocalTime(
        iana_zone="America/Denver",
        capture_ts_utc="2024-07-04T15:15:00+00:00",
        capture_ts_local="2024-07-04T09:15:00-06:00",
        local_date="2024-07-04",
        local_time_hms="09-15-00",
        tz_ambiguous=False,
    )


def test_compute_dest_path_full():
    geo = GeocodeResult(
        geonameid=5574991,
        ascii_name="Boulder",
        place_string="Boulder, Colorado, United States",
        feature_class="P",
        geocode_confidence="nearest_city",
    )
    path = pathing.compute_dest_path(
        Path("Z:/Lib"), geo, _local(), "DJI_0001", ".JPG"
    )
    assert path.startswith("\\\\?\\")
    assert path.endswith(
        r"Boulder, Colorado, United States\2024-07-04\2024-07-04_09-15-00_DJI_0001.JPG"
    )


def test_compute_dest_path_truncates_only_city():
    geo = GeocodeResult(
        geonameid=1,
        ascii_name="C" * 60,
        place_string=("C" * 60) + ", Region, Country",
        feature_class="P",
        geocode_confidence="nearest_city",
    )
    path = pathing.compute_dest_path(Path("Z:/Lib"), geo, _local(), "DJI_0001", ".JPG")
    assert ("C" * 40 + ", Region, Country") in path


def test_compute_dest_path_geonameid_fallback():
    geo = GeocodeResult(
        geonameid=5574991,
        ascii_name="",
        place_string=None,
        feature_class=None,
        geocode_confidence="fallback",
    )
    path = pathing.compute_dest_path(Path("Z:/Lib"), geo, _local(), "DJI_0001", ".JPG")
    assert r"\5574991\2024-07-04" in path  # geonameid used as the place folder


def test_compute_dest_path_requires_local_date():
    geo = GeocodeResult(5574991, "Boulder", "Boulder, Colorado, United States", "P", "nearest_city")
    empty = LocalTime(None, None, None, None, None, False)
    with pytest.raises(ValueError):
        pathing.compute_dest_path(Path("Z:/Lib"), geo, empty, "DJI_0001", ".JPG")
