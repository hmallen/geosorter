"""Tests for the directory-aware ingest pre-scan (B10).

`prescan_inbox` partitions an inbox by DCIM subdir (flat / HYPERLAPSE / PANORAMA /
MISC), runs the existing `group_companions` on the flat partition, and links each
`HYPERLAPSE/<seq>_<counter>/` frame directory to its flat render group by counter
plus an mtime-proximity guard. PANORAMA/MISC are routed out as `unclaimed`; a
frame dir with no matching render becomes a logged orphan warning (never a raise,
never a wrong-link).
"""

import os

from geosorter import grouping


def _touch(path, mtime):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    os.utime(path, (mtime, mtime))
    return path


def _names(companions):
    return {(p.name, ctype) for p, ctype in companions}


def _hyperlapse_dir(inbox, counter, *, n_frames, base_mtime):
    """Create DCIM/HYPERLAPSE/001_<counter>/ with n_frames frames; return the dir."""
    d = inbox / "DCIM" / "HYPERLAPSE" / f"001_{counter}"
    for i in range(1, n_frames + 1):
        _touch(d / f"HYPERLAPSE_{i:04d}.JPG", base_mtime + i)
    return d


def test_prescan_links_hyperlapse_frames(tmp_path):
    inbox = tmp_path / "card"
    # Render lives in the flat DCIM/DJI_001/ partition; written shortly AFTER its
    # frames (the real card has the render mtime ~minutes past the last frame).
    render = _touch(
        inbox / "DCIM" / "DJI_001" / "DJI_20240829183426_0021_D.MP4", 1100.0
    )
    _hyperlapse_dir(inbox, "0021", n_frames=3, base_mtime=1000.0)

    paths = [p for p in sorted(inbox.rglob("*")) if p.is_file()]
    result = grouping.prescan_inbox(paths, inbox_root=inbox)

    assert len(result.groups) == 1
    g = result.groups[0]
    assert g.primary == render
    assert g.capture_kind == "hyperlapse"
    assert _names(g.companions) == {
        ("HYPERLAPSE_0001.JPG", "hyperlapse_frame"),
        ("HYPERLAPSE_0002.JPG", "hyperlapse_frame"),
        ("HYPERLAPSE_0003.JPG", "hyperlapse_frame"),
    }
    assert result.unclaimed == []
    assert result.warnings == []


def test_prescan_frames_sorted_by_name(tmp_path):
    inbox = tmp_path / "card"
    _touch(inbox / "DCIM" / "DJI_001" / "DJI_20240829183426_0021_D.MP4", 1100.0)
    _hyperlapse_dir(inbox, "0021", n_frames=5, base_mtime=1000.0)

    paths = [p for p in sorted(inbox.rglob("*")) if p.is_file()]
    g = grouping.prescan_inbox(paths, inbox_root=inbox).groups[0]
    frame_names = [p.name for p, _ in g.companions]
    assert frame_names == sorted(frame_names)  # ascending HYPERLAPSE_0001..0005


def test_prescan_orphan_frame_dir_warns(tmp_path):
    inbox = tmp_path / "card"
    # A normal flat capture for a DIFFERENT counter, plus a frame dir whose counter
    # (0099) has no matching render.
    flat = _touch(
        inbox / "DCIM" / "DJI_001" / "DJI_20240829183426_0007_D.MP4", 1100.0
    )
    _hyperlapse_dir(inbox, "0099", n_frames=3, base_mtime=1000.0)

    paths = [p for p in sorted(inbox.rglob("*")) if p.is_file()]
    result = grouping.prescan_inbox(paths, inbox_root=inbox)

    # The flat capture survives as a normal group; the orphan frame dir warns.
    assert [g.primary for g in result.groups] == [flat]
    assert all(g.capture_kind is None for g in result.groups)
    assert any("0099" in w for w in result.warnings)


def test_prescan_panorama_and_misc_are_unclaimed(tmp_path):
    inbox = tmp_path / "card"
    render = _touch(
        inbox / "DCIM" / "DJI_001" / "DJI_20240829183426_0021_D.MP4", 1100.0
    )
    _hyperlapse_dir(inbox, "0021", n_frames=2, base_mtime=1000.0)
    pano = _touch(inbox / "DCIM" / "PANORAMA" / "001_0002" / "PANO_0001.JPG", 1000.0)
    misc = _touch(inbox / "MISC" / "FC8482.db", 1000.0)

    paths = [p for p in sorted(inbox.rglob("*")) if p.is_file()]
    result = grouping.prescan_inbox(paths, inbox_root=inbox)

    assert [g.primary for g in result.groups] == [render]
    assert set(result.unclaimed) == {pano, misc}
    # Unclaimed paths never leak into a flat group.
    grouped = {p for g in result.groups for p, _ in g.companions} | {
        g.primary for g in result.groups
    }
    assert pano not in grouped and misc not in grouped


def test_prescan_mtime_guard_rejects_far_render(tmp_path):
    inbox = tmp_path / "card"
    # Same counter, but the render's mtime is hours away from the frame dir — the
    # secondary mtime guard must refuse to link (orphan), leaving the render a
    # normal flat capture rather than a wrong-linked hyperlapse.
    render = _touch(
        inbox / "DCIM" / "DJI_001" / "DJI_20240829183426_0021_D.MP4", 1000.0 + 7200
    )
    _hyperlapse_dir(inbox, "0021", n_frames=3, base_mtime=1000.0)

    paths = [p for p in sorted(inbox.rglob("*")) if p.is_file()]
    result = grouping.prescan_inbox(paths, inbox_root=inbox)

    assert [g.primary for g in result.groups] == [render]
    assert result.groups[0].capture_kind is None
    assert result.groups[0].companions == []
    assert any("0021" in w for w in result.warnings)


def test_prescan_flat_only_matches_group_companions(tmp_path):
    inbox = tmp_path / "card"
    mp4 = _touch(inbox / "DCIM" / "DJI_001" / "DJI_20240825165234_0001_D.MP4", 2000.0)
    srt = _touch(inbox / "DCIM" / "DJI_001" / "DJI_20240825165234_0001_D.SRT", 2000.0)
    lrf = _touch(inbox / "DCIM" / "DJI_001" / "DJI_20240825165234_0001_D.LRF", 2000.0)

    paths = [p for p in sorted(inbox.rglob("*")) if p.is_file()]
    result = grouping.prescan_inbox(paths, inbox_root=inbox)

    assert len(result.groups) == 1
    assert result.groups[0].primary == mp4
    assert result.groups[0].capture_kind is None
    assert _names(result.groups[0].companions) == {
        ("DJI_20240825165234_0001_D.SRT", "srt"),
        ("DJI_20240825165234_0001_D.LRF", "lrf"),
    }
    assert result.unclaimed == []
    assert result.warnings == []
