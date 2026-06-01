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
