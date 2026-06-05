"""Tests for lazy, cached derived-asset generation (thumbnails/posters/proxies)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from PIL import Image

from geosorter import derived

FIXTURES = Path(__file__).parent / "fixtures" / "media"


def _probe_codec(path: Path) -> str:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    return out.stdout.strip()


def test_thumbnail_creates_512px_jpeg(tmp_path):
    out = derived.thumbnail(tmp_path, FIXTURES / "dji_photo.jpg")
    assert out.exists()
    with Image.open(out) as img:
        assert img.format == "JPEG"
        assert max(img.size) == 512  # 800x600 source -> 512x384
    assert out.is_relative_to(tmp_path / ".geosorter-cache")


def test_thumbnail_applies_exif_transpose(tmp_path):
    # A 100x50 image tagged Orientation=6 displays rotated to 50x100.
    src = tmp_path / "oriented.jpg"
    img = Image.new("RGB", (100, 50), "red")
    exif = img.getexif()
    exif[0x0112] = 6  # Orientation: rotate 90 CW on display
    img.save(src, exif=exif)

    out = derived.thumbnail(tmp_path, src)
    with Image.open(out) as thumb:
        # exif_transpose must have swapped dimensions: height now exceeds width.
        assert thumb.size[1] > thumb.size[0]


def test_thumbnail_cache_hit_does_not_regenerate(tmp_path):
    out = derived.thumbnail(tmp_path, FIXTURES / "dji_photo.jpg")
    first = out.stat().st_mtime_ns
    again = derived.thumbnail(tmp_path, FIXTURES / "dji_photo.jpg")
    assert again == out
    assert again.stat().st_mtime_ns == first  # not regenerated


def test_poster_extracts_frame(tmp_path):
    out = derived.poster(tmp_path, FIXTURES / "h264_tiny.mp4")
    assert out.exists()
    with Image.open(out) as img:
        assert img.format == "JPEG"
        assert img.size == (320, 240)


def test_proxy_transcodes_hevc_to_h264(tmp_path):
    out = derived.proxy(tmp_path, FIXTURES / "h265_tiny.mp4", "h265")
    assert out.exists()
    assert out != FIXTURES / "h265_tiny.mp4"
    assert _probe_codec(out) == "h264"


def test_proxy_passthrough_for_h264(tmp_path):
    src = FIXTURES / "h264_tiny.mp4"
    out = derived.proxy(tmp_path, src, "h264")
    assert out == src  # no transcode for already-playable codecs


def test_proxy_cache_hit_does_not_regenerate(tmp_path):
    out = derived.proxy(tmp_path, FIXTURES / "h265_tiny.mp4", "h265")
    first = out.stat().st_mtime_ns
    again = derived.proxy(tmp_path, FIXTURES / "h265_tiny.mp4", "h265")
    assert again.stat().st_mtime_ns == first


def test_preview_caps_long_edge_at_1920(tmp_path):
    src = tmp_path / "big.jpg"
    Image.new("RGB", (4000, 3000), "red").save(src)
    out = derived.preview(tmp_path, src)
    assert out.exists()
    with Image.open(out) as img:
        assert img.format == "JPEG"
        assert max(img.size) == 1920  # 4000x3000 -> 1920x1440
    assert out.is_relative_to(tmp_path / ".geosorter-cache" / "previews")


def test_preview_passthrough_small(tmp_path):
    # An 800px source is already under the 1920 cap; no upscale.
    out = derived.preview(tmp_path, FIXTURES / "dji_photo.jpg")
    with Image.open(out) as img:
        assert max(img.size) == 800


def test_find_hugin_returns_none_when_any_tool_missing(monkeypatch):
    monkeypatch.setattr(derived.shutil, "which", lambda name: None)
    assert derived.find_hugin() is None


def test_find_hugin_returns_all_tools_on_path(monkeypatch):
    monkeypatch.setattr(derived.shutil, "which", lambda name: f"/usr/bin/{Path(name).name}")
    tools = derived.find_hugin()
    assert tools is not None
    assert set(tools) == set(derived._HUGIN_TOOLS)


def test_find_hugin_uses_bin_dir(monkeypatch, tmp_path):
    seen: list[str] = []

    def _which(name):
        seen.append(name)
        return name  # pretend each lookup resolves

    monkeypatch.setattr(derived.shutil, "which", _which)
    bindir = tmp_path / "hbin"
    tools = derived.find_hugin(bindir)
    assert tools is not None
    assert all(str(bindir) in s for s in seen)  # looked under the bin dir, not bare PATH


def test_run_hugin_raises_stitchfailed_on_nonzero(monkeypatch):
    class _Result:
        returncode = 1
        stderr = "control points lost"

    monkeypatch.setattr(derived.subprocess, "run", lambda *a, **k: _Result())
    with pytest.raises(derived.StitchFailed):
        derived._run_hugin(["cpfind", "x"])


def test_run_hugin_wraps_timeout_as_stitchfailed(monkeypatch):
    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="cpfind", timeout=1)

    monkeypatch.setattr(derived.subprocess, "run", _timeout)
    with pytest.raises(derived.StitchFailed):
        derived._run_hugin(["cpfind", "x"], timeout=1)


def _make_pano_tiles(library_root: Path, n: int = 3):
    """Create a primary tile + (n-1) frame tiles under library_root/pano/."""
    d = library_root / "pano"
    d.mkdir(parents=True)
    primary = d / "PANO_0001.JPG"
    Image.new("RGB", (400, 300), "green").save(primary)
    frames = []
    for i in range(2, n + 1):
        f = d / f"PANO_{i:04d}.JPG"
        Image.new("RGB", (400, 300), "green").save(f)
        frames.append(f)
    return primary, frames


def _fake_hugin_tools(hugin_bin_dir=None):
    return {name: name for name in derived._HUGIN_TOOLS}


def _fake_run_factory(fill: str = "skyblue", size: tuple[int, int] = (3000, 1400)):
    """A fake _run_hugin that emits a known out.tif on the --stitching step."""
    calls: list[list[str]] = []

    def _run(cmd, *, timeout: int = derived.STITCH_STEP_TIMEOUT_S):
        calls.append(cmd)
        if "--stitching" in cmd:
            prefix = next(a.split("=", 1)[1] for a in cmd if a.startswith("--prefix="))
            Image.new("RGB", size, fill).save(prefix + ".tif")

    return _run, calls


def test_panorama_stitch_success_caches_and_is_idempotent(tmp_path, monkeypatch):
    library_root = tmp_path / "lib"
    primary, frames = _make_pano_tiles(library_root)
    monkeypatch.setattr(derived, "find_hugin", _fake_hugin_tools)
    run, calls = _fake_run_factory()
    monkeypatch.setattr(derived, "_run_hugin", run)

    out = derived.panorama_stitch(library_root, primary, frames)
    assert out.exists()
    with Image.open(out) as img:
        assert img.format == "JPEG"
    assert out.is_relative_to(library_root / ".geosorter-cache" / "stitch")
    first_calls = len(calls)
    assert first_calls >= 6  # the full 6-step pipeline ran

    out2 = derived.panorama_stitch(library_root, primary, frames)
    assert out2 == out
    assert len(calls) == first_calls  # cache hit -> no further hugin invocations


def test_panorama_stitch_missing_hugin_raises(tmp_path, monkeypatch):
    library_root = tmp_path / "lib"
    primary, frames = _make_pano_tiles(library_root)
    monkeypatch.setattr(derived, "find_hugin", lambda hugin_bin_dir=None: None)
    with pytest.raises(derived.HuginNotFound):
        derived.panorama_stitch(library_root, primary, frames)


def test_panorama_stitch_pipeline_failure_raises_and_caches_nothing(tmp_path, monkeypatch):
    library_root = tmp_path / "lib"
    primary, frames = _make_pano_tiles(library_root)
    monkeypatch.setattr(derived, "find_hugin", _fake_hugin_tools)

    def _boom(cmd, *, timeout: int = derived.STITCH_STEP_TIMEOUT_S):
        raise derived.StitchFailed("cpfind failed")

    monkeypatch.setattr(derived, "_run_hugin", _boom)
    with pytest.raises(derived.StitchFailed):
        derived.panorama_stitch(library_root, primary, frames)
    stitch_cache = library_root / ".geosorter-cache" / "stitch"
    assert not (stitch_cache.exists() and list(stitch_cache.rglob("*.jpg")))


def test_panorama_stitch_rejects_degenerate_black_output(tmp_path, monkeypatch):
    library_root = tmp_path / "lib"
    primary, frames = _make_pano_tiles(library_root)
    monkeypatch.setattr(derived, "find_hugin", _fake_hugin_tools)
    run, _ = _fake_run_factory(fill="black")  # ~100% black void
    monkeypatch.setattr(derived, "_run_hugin", run)
    with pytest.raises(derived.StitchFailed):
        derived.panorama_stitch(library_root, primary, frames)


def test_panorama_stitch_real_hugin_e2e(tmp_path):
    # End-to-end against the REAL Hugin pipeline + real DJI tiles. Skips unless both
    # Hugin is installed AND a local panorama tile set is staged (mirrors the
    # committed-media e2e convention) — so the default suite stays fast and green.
    if derived.find_hugin() is None:
        pytest.skip("Hugin CLI not found on PATH / hugin_bin_dir")
    pano_dir = FIXTURES / "local" / "panorama"
    tiles = sorted(pano_dir.glob("PANO_*.JPG")) if pano_dir.is_dir() else []
    if len(tiles) < 2:
        pytest.skip(f"no local panorama tiles at {pano_dir}")

    out = derived.panorama_stitch(tmp_path, tiles[0], tiles[1:])
    assert out.is_file()
    derived._stitch_gate(out)  # the real output passes the equirectangular gate


def test_atomic_write_failure_publishes_nothing(tmp_path):
    # A failed generation must never leave a half-written cache file at `out`,
    # nor a leftover temp in the cache dir (concurrent-request corruption guard).
    out = tmp_path / ".geosorter-cache" / "thumbs" / "x.jpg"

    def boom(dest):
        dest.write_bytes(b"partial-bytes")  # wrote to the temp...
        raise RuntimeError("generation failed")

    with pytest.raises(RuntimeError):
        derived._atomic_write(out, boom)
    assert not out.exists()  # ...but `out` was never published
    assert list(out.parent.iterdir()) == []  # temp cleaned up
