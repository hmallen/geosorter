"""Tests for the inbox counter (files scanned vs DJI capture groups)."""

from __future__ import annotations

from geosorter import inbox


def _add(d, name, data=b"x"):
    p = d / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_none_inbox_is_zero():
    assert inbox.count_inbox(None) == inbox.InboxCount(0, 0)


def test_missing_dir_is_zero(tmp_path):
    assert inbox.count_inbox(tmp_path / "nope") == inbox.InboxCount(0, 0)


def test_empty_dir_is_zero(tmp_path):
    box = tmp_path / "inbox"
    box.mkdir()
    assert inbox.count_inbox(box) == inbox.InboxCount(0, 0)


def test_counts_dji_capture_and_files(tmp_path):
    box = tmp_path / "inbox"
    box.mkdir()
    _add(box, "DJI_0001.MP4")
    _add(box, "DJI_0001.SRT")  # companion of the same capture
    _add(box, "notes.txt")     # non-DJI: counts as a file, not a capture
    assert inbox.count_inbox(box) == inbox.InboxCount(files=3, captures=1)


def test_two_distinct_dji_photos(tmp_path):
    box = tmp_path / "inbox"
    box.mkdir()
    _add(box, "DJI_0001.JPG")
    _add(box, "DJI_0002.JPG")
    assert inbox.count_inbox(box) == inbox.InboxCount(files=2, captures=2)


def test_hyperlapse_render_and_frames_count_as_one_capture(tmp_path):
    # A render + its 5 source frames is ONE capture (the pre-scan links them), even
    # though it is 6 files — so the badge reads "1 capture", not "1 + 5 singles".
    box = tmp_path / "inbox"
    box.mkdir()
    render = _add(box, "DCIM/DJI_001/DJI_20240829183426_0021_D.MP4")
    import os
    for i in range(1, 6):
        f = _add(box, f"DCIM/HYPERLAPSE/001_0021/HYPERLAPSE_{i:04d}.JPG")
        os.utime(f, (1000.0 + i, 1000.0 + i))
    os.utime(render, (1100.0, 1100.0))
    assert inbox.count_inbox(box) == inbox.InboxCount(files=6, captures=1)


def test_panorama_tiles_count_as_one_capture(tmp_path):
    # A PANORAMA dir of N tiles is ONE capture unit (the pre-scan models it), even
    # though it is N files — the badge reads "1 capture", not "N singles".
    box = tmp_path / "inbox"
    box.mkdir()
    for i in range(1, 8):
        _add(box, f"DCIM/PANORAMA/001_0002/PANO_{i:04d}.JPG")
    assert inbox.count_inbox(box) == inbox.InboxCount(files=7, captures=1)


# --------------------------------------------------------------------------- #
# list_inbox — per-capture-group enumeration for the import-selection UI.
def test_list_inbox_none_and_missing(tmp_path):
    assert inbox.list_inbox(None) == []
    assert inbox.list_inbox(tmp_path / "nope") == []


def test_list_inbox_empty_dir(tmp_path):
    box = tmp_path / "inbox"
    box.mkdir()
    assert inbox.list_inbox(box) == []


def test_list_inbox_groups_with_companions(tmp_path):
    box = tmp_path / "inbox"
    box.mkdir()
    # One video capture (primary + .SRT companion) and one standalone photo, both in
    # a DCIM subdir; the id is the inbox-relative POSIX primary path.
    _add(box, "DCIM/100MEDIA/DJI_0001.MP4")
    _add(box, "DCIM/100MEDIA/DJI_0001.SRT")  # companion of DJI_0001
    _add(box, "DCIM/100MEDIA/DJI_0002.JPG")
    groups = inbox.list_inbox(box)
    assert [g.id for g in groups] == [
        "DCIM/100MEDIA/DJI_0001.MP4",
        "DCIM/100MEDIA/DJI_0002.JPG",
    ]
    g0 = groups[0]
    assert g0.dir == "DCIM/100MEDIA"
    assert g0.name == "DJI_0001.MP4"
    assert g0.capture_kind is None
    assert g0.file_count == 2  # primary + the .SRT companion
    assert groups[1].file_count == 1


def test_list_inbox_root_level_group_has_empty_dir(tmp_path):
    box = tmp_path / "inbox"
    box.mkdir()
    _add(box, "DJI_0001.JPG")
    g = inbox.list_inbox(box)[0]
    assert g.id == "DJI_0001.JPG"
    assert g.dir == ""


def test_list_inbox_hyperlapse_is_one_group(tmp_path):
    import os

    box = tmp_path / "inbox"
    box.mkdir()
    render = _add(box, "DCIM/DJI_001/DJI_20240829183426_0021_D.MP4")
    for i in range(1, 6):
        f = _add(box, f"DCIM/HYPERLAPSE/001_0021/HYPERLAPSE_{i:04d}.JPG")
        os.utime(f, (1000.0 + i, 1000.0 + i))
    os.utime(render, (1100.0, 1100.0))
    groups = inbox.list_inbox(box)
    assert len(groups) == 1
    g = groups[0]
    assert g.capture_kind == "hyperlapse"
    assert g.file_count == 6  # render + 5 frames
