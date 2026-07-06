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


# --------------------------------------------------------------------------- #
# library_rel_key — collision-proof, resolve-free cache/URL key
# --------------------------------------------------------------------------- #
def test_library_rel_key_strips_prefix_to_posix():
    key = pathing.library_rel_key(r"Z:\Lib", r"Z:\Lib\Boulder\2024-07-04\DJI_0001.JPG")
    assert key == "Boulder/2024-07-04/DJI_0001.JPG"


def test_library_rel_key_handles_long_path_prefix():
    # Stored dest_paths carry the Windows \\?\ long-path prefix; it is dropped.
    dest = "\\\\?\\" + r"Z:\Lib\A\DJI_0001.JPG"  # real 4-char \\?\ prefix
    assert pathing.library_rel_key(r"Z:\Lib", dest) == "A/DJI_0001.JPG"


def test_library_rel_key_is_case_insensitive_on_windows():
    # A drive-letter / case mismatch between the configured root and the stored path
    # must still strip cleanly (NTFS is case-insensitive).
    key = pathing.library_rel_key(r"Z:\Lib", r"z:\lib\A\DJI_0001.JPG")
    assert key == "A/DJI_0001.JPG"


def test_library_rel_key_distinguishes_same_basename_across_folders():
    # The whole point: two DJI_0001.JPG in different folders get DISTINCT keys
    # (the old bare-filename fallback collided them -> wrong thumbnail).
    a = pathing.library_rel_key(r"Z:\Lib", r"Z:\Lib\A\DJI_0001.JPG")
    b = pathing.library_rel_key(r"Z:\Lib", r"Z:\Lib\B\DJI_0001.JPG")
    assert a != b


def test_library_rel_key_outside_root_uses_full_path_not_bare_name():
    # A path on a different drive (or otherwise not under library_root) falls back to
    # a drive-sanitized FULL path, never a bare filename (collision-free).
    key = pathing.library_rel_key(r"Z:\Lib", r"C:\other\DJI_0001.JPG")
    assert key.endswith("DJI_0001.JPG")
    assert "/" in key  # not a bare filename
    assert key != "DJI_0001.JPG"


def test_strip_long_prefix_drive_and_unc_forms():
    # Drive-letter form: the 4-char prefix drops cleanly.
    assert pathing.strip_long_prefix("\\\\?\\Z:\\Lib\\A\\f.JPG") == "Z:\\Lib\\A\\f.JPG"
    # UNC form: \\?\UNC\server\share -> \\server\share (a naive 4-char strip
    # would leave the broken "UNC\server\share").
    assert (
        pathing.strip_long_prefix("\\\\?\\UNC\\nas\\media\\Lib\\f.JPG")
        == "\\\\nas\\media\\Lib\\f.JPG"
    )
    # Unprefixed paths pass through untouched.
    assert pathing.strip_long_prefix("Z:\\Lib\\f.JPG") == "Z:\\Lib\\f.JPG"
    assert pathing.strip_long_prefix("\\\\nas\\media\\f.JPG") == "\\\\nas\\media\\f.JPG"


def test_add_long_prefix_drive_and_unc_forms():
    assert pathing.add_long_prefix("Z:\\Lib\\f.JPG") == "\\\\?\\Z:\\Lib\\f.JPG"
    # UNC paths need the \\?\UNC\ form — "\\?\" + "\\server\..." is rejected by Windows.
    assert (
        pathing.add_long_prefix("\\\\nas\\media\\Lib\\f.JPG")
        == "\\\\?\\UNC\\nas\\media\\Lib\\f.JPG"
    )
    # Already-prefixed paths (either form) pass through unchanged.
    assert pathing.add_long_prefix("\\\\?\\Z:\\f.JPG") == "\\\\?\\Z:\\f.JPG"
    assert (
        pathing.add_long_prefix("\\\\?\\UNC\\nas\\media\\f.JPG")
        == "\\\\?\\UNC\\nas\\media\\f.JPG"
    )


def test_strip_add_round_trip():
    for original in ("Z:\\Lib\\A\\f.JPG", "\\\\nas\\media\\Lib\\A\\f.JPG"):
        assert pathing.strip_long_prefix(pathing.add_long_prefix(original)) == original


def test_compute_dest_path_unc_library_root():
    # A UNC library_root (documented in config) must produce the valid
    # \\?\UNC\server\share\... long form, not the malformed \\?\\\server\...
    geocode = GeocodeResult(
        geonameid=1,
        ascii_name="Boulder",
        place_string="Boulder, Colorado, United States",
        feature_class="P",
        geocode_confidence="nearest_city",
    )
    local = LocalTime(
        iana_zone="America/Denver",
        capture_ts_utc="2024-04-12T20:30:05+00:00",
        capture_ts_local="2024-04-12T14:30:05-06:00",
        local_date="2024-04-12",
        local_time_hms="14-30-05",
        tz_ambiguous=False,
    )
    dest = pathing.compute_dest_path(
        Path("\\\\nas\\media\\Library"), geocode, local, "DJI_0001", ".JPG"
    )
    assert dest.startswith("\\\\?\\UNC\\nas\\media\\Library")
    assert pathing.strip_long_prefix(dest).startswith("\\\\nas\\media\\Library")
