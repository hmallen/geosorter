"""Tests for the DJI MISC-catalog ratings parser (B11).

`misc_parser.read_ratings` opens a DJI catalog DB read-only, reads only the
scalar `gis_info_table.file_name`/`star` columns, and is fully fail-safe (any
error -> `{}`) so it can never abort the crash-safe move path. `select_catalog`
picks the live catalog by basename overlap against the organized files.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from geosorter import misc_parser


def _make_catalog(path: Path, rows: list[tuple[str, int]], *, with_blob: bool = True) -> Path:
    """Build a minimal DJI-shaped catalog DB at ``path``.

    ``rows`` are ``(file_name, star)`` pairs for ``gis_info_table``. When
    ``with_blob`` an ``image_info_table`` with an EXIF BLOB row is added to prove
    the parser never reads it.
    """
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE gis_info_table (file_name TEXT, star INT)")
    conn.executemany("INSERT INTO gis_info_table(file_name, star) VALUES (?,?)", rows)
    if with_blob:
        conn.execute("CREATE TABLE image_info_table (exif BLOB)")
        conn.execute("INSERT INTO image_info_table(exif) VALUES (?)", (b"\x00\x01\x02EXIF",))
    conn.commit()
    conn.close()
    return path


def test_read_ratings_happy(tmp_path):
    db = _make_catalog(
        tmp_path / "FC.db",
        [
            ("/mnt/media_rw/sdcard0/DCIM/DJI_001/DJI_0001.MP4", 3),
            ("/mnt/media_rw/sdcard0/DCIM/PANORAMA/001_0002/PANO_0001.JPG", 0),
        ],
    )
    assert misc_parser.read_ratings(db) == {"dji_0001.mp4": 3, "pano_0001.jpg": 0}


def test_read_ratings_corrupt_returns_empty(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"\x7f\x45random-not-sqlite" * 8)
    assert misc_parser.read_ratings(bad) == {}


def test_read_ratings_missing_file_returns_empty(tmp_path):
    assert misc_parser.read_ratings(tmp_path / "nope.db") == {}


def test_read_ratings_no_gis_table_returns_empty(tmp_path):
    # A valid sqlite DB that has only image_info_table (no gis_info_table) -> {}.
    db = tmp_path / "noGis.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE image_info_table (exif BLOB)")
    conn.execute("INSERT INTO image_info_table(exif) VALUES (?)", (b"blob",))
    conn.commit()
    conn.close()
    assert misc_parser.read_ratings(db) == {}


def test_read_ratings_skips_null_and_blank(tmp_path):
    db = _make_catalog(
        tmp_path / "FC.db",
        [("/x/DJI_0001.MP4", 2), ("", 4)],
        with_blob=False,
    )
    # A NULL star or blank file_name row is skipped, not an error.
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO gis_info_table(file_name, star) VALUES (?,?)", ("/x/DJI_0002.MP4", None))
    conn.commit()
    conn.close()
    assert misc_parser.read_ratings(db) == {"dji_0001.mp4": 2}


# --------------------------------------------------------------------------- #
# select_catalog
# --------------------------------------------------------------------------- #
def test_select_catalog_picks_live_over_stale(tmp_path):
    # Organized dests are renamed <date>_<time>_<orig>; the live catalog's basenames
    # are suffixes of them, the stale catalog's are not.
    organized = ["2024-08-25_16-52-34_dji_0001.mp4", "2024-08-25_17-00-00_pano_0001.jpg"]
    live = {"dji_0001.mp4": 4, "pano_0001.jpg": 0}
    stale = {"dji_9999.mov": 0, "dji_8888.mov": 0}
    catalogs = {Path("live.db"): live, Path("stale.db"): stale}
    assert misc_parser.select_catalog(catalogs, organized) == Path("live.db")


def test_select_catalog_tie_returns_none(tmp_path):
    organized = ["2024-08-25_16-52-34_dji_0001.mp4"]
    a = {"dji_0001.mp4": 1}
    b = {"dji_0001.mp4": 2}
    assert misc_parser.select_catalog({Path("a.db"): a, Path("b.db"): b}, organized) is None


def test_select_catalog_all_zero_returns_none(tmp_path):
    organized = ["2024-08-25_16-52-34_dji_0001.mp4"]
    stale = {"dji_9999.mov": 0}
    assert misc_parser.select_catalog({Path("stale.db"): stale}, organized) is None


def test_select_catalog_empty_returns_none(tmp_path):
    assert misc_parser.select_catalog({}, ["x.mp4"]) is None
