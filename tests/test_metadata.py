"""Tests for the pyexiftool daemon metadata extractor + codec detection.

Committed fixtures (``fixtures/media/``) are sanitized + tiny:
* ``dji_photo.jpg`` — real DJI FC7303 EXIF/MakerNotes, GPS offset to a
  non-identifying value, downscaled to 800px.
* ``h264_tiny.mp4`` / ``h265_tiny.mp4`` — 320x240 1s clips with real ``avc1`` /
  ``hvc1`` codecs for portable codec/dimension checks.
* ``h265_tiny.SRT`` — sidecar for the H.265 clip: exercises the no-embedded-GPS
  SRT-fallback path without a large real video.

Real DJI originals (``fixtures/media/local/``, gitignored) are exercised by the
``local_media``-guarded tests, which skip when the files are absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from geosorter.metadata import (
    ExifToolVersionError,
    MediaMetadata,
    MetadataExtractor,
    detect_codec,
)

MEDIA = Path(__file__).parent / "fixtures" / "media"


@pytest.fixture(scope="module")
def extractor():
    with MetadataExtractor() as ex:
        yield ex


def test_photo_exif_gps_ts_dims(extractor):
    md = extractor.extract(MEDIA / "dji_photo.jpg")
    assert md.media_type == "photo"
    assert md.gps_source == "exif"
    assert md.lat == pytest.approx(43.0148385)
    assert md.lon == pytest.approx(-87.0673939, abs=1e-4)
    assert md.exif_gps == pytest.approx((43.0148385, -87.0673939), abs=1e-4)
    assert md.capture_ts_raw == "2023:01:07 17:44:36"
    assert md.capture_ts_source_tag == "EXIF:DateTimeOriginal"
    assert md.width == 800
    assert md.height == 600
    assert md.codec is None  # photos have no codec


def test_codec_h264(extractor):
    md = extractor.extract(MEDIA / "h264_tiny.mp4")
    assert md.media_type == "video"
    assert md.codec == "h264"
    assert md.codec_raw.lower() == "avc1"
    assert md.width == 320
    assert md.height == 240
    assert md.duration_s == pytest.approx(1.0, abs=0.2)


def test_codec_h265(extractor):
    md = extractor.extract(MEDIA / "h265_tiny.mp4")
    assert md.codec == "h265"
    assert md.codec_raw.lower() == "hvc1"


def test_video_srt_fallback_audit(extractor):
    # The H.265 clip has no embedded GPS; GPS must come from the sibling SRT,
    # and both sources are retained for audit (exif_gps None, srt_gps set).
    md = extractor.extract(MEDIA / "h265_tiny.mp4")
    assert md.exif_gps is None
    assert md.gps_source == "srt"
    assert md.srt_gps == pytest.approx((14.798240, -65.691450))
    assert md.lat == pytest.approx(14.798240)


def test_version_gate_aborts_below_minimum():
    # An impossibly-high minimum must abort on context enter.
    with pytest.raises(ExifToolVersionError):
        with MetadataExtractor(min_version=(999, 0)):
            pass


def test_detect_codec_tag_mapping():
    assert detect_codec("hvc1", MEDIA / "h265_tiny.mp4") == ("h265", "hvc1")
    assert detect_codec("hev1", MEDIA / "h265_tiny.mp4") == ("h265", "hev1")
    assert detect_codec("avc1", MEDIA / "h264_tiny.mp4") == ("h264", "avc1")


def test_detect_codec_ffprobe_fallback():
    # No usable tag -> ffprobe inspects the real stream.
    codec, raw = detect_codec(None, MEDIA / "h264_tiny.mp4")
    assert codec == "h264"
    assert raw == "h264"


# --- Real DJI originals (skip when local fixtures absent) --------------------


def test_real_h264_embedded_gps(extractor, local_media):
    path = local_media("dji_h264_gps.mp4")
    md = extractor.extract(path)
    assert md.media_type == "video"
    assert md.codec == "h264"
    assert md.gps_source == "exif"
    assert md.lat == pytest.approx(4.8091, abs=1e-3)
    assert md.exif_gps is not None


def test_real_h265_srt_fallback(extractor, local_media):
    path = local_media("dji_h265_nogps.mp4")
    md = extractor.extract(path)
    assert md.codec == "h265"
    assert md.exif_gps is None
    assert md.gps_source == "srt"
    assert md.srt_gps is not None


def test_extract_returns_dataclass(extractor):
    md = extractor.extract(MEDIA / "dji_photo.jpg")
    assert isinstance(md, MediaMetadata)
