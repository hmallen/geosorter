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
