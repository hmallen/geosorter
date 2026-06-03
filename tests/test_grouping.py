"""Tests for companion grouping (DJI base name + counter + mtime proximity)."""

import os

from geosorter import grouping


def _touch(path, mtime):
    path.write_bytes(b"x")
    os.utime(path, (mtime, mtime))
    return path


def _by_name(companions):
    return {(p.name, ctype) for p, ctype in companions}


def test_group_photo_with_dng(tmp_path):
    jpg = _touch(tmp_path / "DJI_0001.JPG", 1000.0)
    dng = _touch(tmp_path / "DJI_0001.DNG", 1000.0)
    groups = grouping.group_companions([jpg, dng])
    assert len(groups) == 1
    assert groups[0].primary == jpg
    assert _by_name(groups[0].companions) == {("DJI_0001.DNG", "dng")}


def test_group_video_with_srt_and_lrf(tmp_path):
    mp4 = _touch(tmp_path / "DJI_0002.MP4", 2000.0)
    srt = _touch(tmp_path / "DJI_0002.SRT", 2000.0)
    lrf = _touch(tmp_path / "DJI_0002.LRF", 2000.0)
    groups = grouping.group_companions([mp4, srt, lrf])
    assert len(groups) == 1
    assert groups[0].primary == mp4
    assert _by_name(groups[0].companions) == {
        ("DJI_0002.SRT", "srt"),
        ("DJI_0002.LRF", "lrf"),
    }


def test_split_video_continuation_grouped_by_name(tmp_path):
    # Continuation segment groups by base name even though its mtime is far off.
    mp4 = _touch(tmp_path / "DJI_0003.MP4", 3000.0)
    cont = _touch(tmp_path / "DJI_0003_001.MP4", 3000.0 + 600)  # 10 min later
    srt = _touch(tmp_path / "DJI_0003.SRT", 3000.0)
    groups = grouping.group_companions([mp4, cont, srt])
    assert len(groups) == 1
    assert groups[0].primary == mp4
    assert _by_name(groups[0].companions) == {
        ("DJI_0003_001.MP4", "other"),
        ("DJI_0003.SRT", "srt"),
    }


def test_sidecar_outside_mtime_window_excluded(tmp_path):
    mp4 = _touch(tmp_path / "DJI_0004.MP4", 4000.0)
    dng = _touch(tmp_path / "DJI_0004.DNG", 4000.0 + 100)  # 100 s away
    groups = grouping.group_companions([mp4, dng])
    assert len(groups) == 1
    assert groups[0].companions == []


def test_distinct_captures_form_distinct_groups(tmp_path):
    a = _touch(tmp_path / "DJI_0005.JPG", 5000.0)
    b = _touch(tmp_path / "DJI_0006.JPG", 6000.0)
    groups = grouping.group_companions([a, b])
    assert {g.primary.name for g in groups} == {"DJI_0005.JPG", "DJI_0006.JPG"}


# Modern DJI naming: DJI_<14-digit-timestamp>_<counter>_<lens>, e.g.
# DJI_20240825165234_0001_D.MP4. The lens letter (_D/_W/_T/_S) is part of the
# grouping key, so distinct lenses are distinct captures.


def test_group_modern_video_with_srt_and_lrf(tmp_path):
    mp4 = _touch(tmp_path / "DJI_20240825165234_0001_D.MP4", 2000.0)
    srt = _touch(tmp_path / "DJI_20240825165234_0001_D.SRT", 2000.0)
    lrf = _touch(tmp_path / "DJI_20240825165234_0001_D.LRF", 2000.0)
    groups = grouping.group_companions([mp4, srt, lrf])
    assert len(groups) == 1
    assert groups[0].primary == mp4
    assert _by_name(groups[0].companions) == {
        ("DJI_20240825165234_0001_D.SRT", "srt"),
        ("DJI_20240825165234_0001_D.LRF", "lrf"),
    }


def test_modern_lens_suffixes_are_separate_captures(tmp_path):
    lens_d = _touch(tmp_path / "DJI_20240825165234_0001_D.MP4", 2000.0)
    lens_w = _touch(tmp_path / "DJI_20240825165234_0001_W.MP4", 2000.0)
    groups = grouping.group_companions([lens_d, lens_w])
    assert {g.primary.name for g in groups} == {
        "DJI_20240825165234_0001_D.MP4",
        "DJI_20240825165234_0001_W.MP4",
    }


def test_modern_split_video_continuation_grouped_by_name(tmp_path):
    mp4 = _touch(tmp_path / "DJI_20240825165234_0001_D.MP4", 3000.0)
    cont = _touch(tmp_path / "DJI_20240825165234_0001_D_001.MP4", 3000.0 + 600)
    groups = grouping.group_companions([mp4, cont])
    assert len(groups) == 1
    assert groups[0].primary == mp4
    assert _by_name(groups[0].companions) == {
        ("DJI_20240825165234_0001_D_001.MP4", "other"),
    }
