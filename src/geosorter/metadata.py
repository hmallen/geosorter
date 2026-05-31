"""DJI media metadata extraction.

Runs ExifTool in **persistent daemon mode** (``-stay_open``) via ``pyexiftool``,
instantiated once per run as a context manager. Extracts signed-decimal GPS,
capture timestamp (raw value + the source tag it came from), pixel dimensions,
duration, and the video codec. For videos that lack embedded GPS, the sibling
``.SRT`` telemetry sidecar is consulted (see :mod:`geosorter.srt_parser`) and
**both** the EXIF and SRT coordinates are retained for audit.

Security: a startup check aborts if ExifTool is older than 12.24 (CVE-2021-22204).
All ExifTool access is via the daemon; the ``ffprobe`` codec fallback uses a
list-form subprocess (never ``shell=True``).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from exiftool import ExifToolHelper

from geosorter.srt_parser import parse_srt

# CVE-2021-22204: arbitrary code execution via crafted image in ExifTool < 12.24.
MIN_EXIFTOOL_VERSION: tuple[int, int] = (12, 24)

_CODEC_MAP = {"avc1": "h264", "hvc1": "h265", "hev1": "h265"}

# ExifTool tags requested for every file. Absent tags are simply omitted from
# the returned dict (photos carry no QuickTime:* keys and vice versa).
_TAGS = [
    "File:MIMEType",
    "Composite:GPSLatitude",
    "Composite:GPSLongitude",
    "EXIF:DateTimeOriginal",
    "EXIF:CreateDate",
    "QuickTime:CreateDate",
    "File:ImageWidth",
    "File:ImageHeight",
    "QuickTime:ImageWidth",
    "QuickTime:ImageHeight",
    "EXIF:ExifImageWidth",
    "EXIF:ExifImageHeight",
    "QuickTime:Duration",
    "QuickTime:CompressorID",
]

# Capture-timestamp source tags, in preference order.
_TS_TAGS = ("EXIF:DateTimeOriginal", "QuickTime:CreateDate", "EXIF:CreateDate")
_WIDTH_TAGS = ("File:ImageWidth", "QuickTime:ImageWidth", "EXIF:ExifImageWidth")
_HEIGHT_TAGS = ("File:ImageHeight", "QuickTime:ImageHeight", "EXIF:ExifImageHeight")


class ExifToolVersionError(RuntimeError):
    """ExifTool on PATH is older than the security-mandated minimum."""


@dataclass(frozen=True)
class MediaMetadata:
    """Capture metadata for one DJI photo or video.

    ``gps_source`` is the provenance of the chosen ``lat``/``lon``:
    ``'exif'`` | ``'srt'`` | ``'srt_partial'`` | ``'none'``. ``exif_gps`` and
    ``srt_gps`` retain each source independently for audit (either may be set
    even when it was not the chosen source).
    """

    media_type: str  # 'photo' | 'video'
    lat: float | None
    lon: float | None
    gps_source: str
    capture_ts_raw: str | None
    capture_ts_source_tag: str | None
    width: int | None
    height: int | None
    duration_s: float | None
    codec: str | None  # 'h264' | 'h265' | None
    codec_raw: str | None
    exif_gps: tuple[float, float] | None
    srt_gps: tuple[float, float] | None


def _ffprobe_codec(path: Path) -> str | None:
    """Return the ffprobe ``codec_name`` of the first video stream, or None.

    List-form subprocess; never raises on a non-zero exit or missing ffprobe.
    """
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    name = proc.stdout.strip()
    return name or None


def detect_codec(raw: str | None, path: str | Path) -> tuple[str | None, str | None]:
    """Map a raw codec tag to ``'h264'``/``'h265'``, falling back to ffprobe.

    ``raw`` is typically ExifTool's ``CompressorID`` (``avc1``/``hvc1``). When it
    is missing or a proprietary/unrecognized string, ``ffprobe`` inspects the
    actual stream. Returns ``(normalized_codec, raw_or_probe_string)``.
    """
    if raw:
        key = str(raw).strip().lower()
        if key in _CODEC_MAP:
            return _CODEC_MAP[key], str(raw)
    name = _ffprobe_codec(Path(path))
    if name:
        low = name.lower()
        if low in ("h264", "avc", "avc1"):
            return "h264", name
        if low in ("hevc", "h265", "hvc1", "hev1"):
            return "h265", name
    return None, (str(raw) if raw else name)


def _find_srt(path: Path) -> Path | None:
    for ext in (".SRT", ".srt"):
        candidate = path.with_suffix(ext)
        if candidate.exists():
            return candidate
    return None


def _coalesce(tags: dict, keys: tuple[str, ...]):
    for key in keys:
        if tags.get(key) is not None:
            return key, tags[key]
    return None, None


class MetadataExtractor:
    """Context manager wrapping a single persistent ExifTool daemon.

    Instantiate once per run::

        with MetadataExtractor() as extractor:
            for path in files:
                md = extractor.extract(path)
    """

    def __init__(self, *, min_version: tuple[int, int] = MIN_EXIFTOOL_VERSION):
        self._min_version = min_version
        self._et: ExifToolHelper | None = None

    def __enter__(self) -> MetadataExtractor:
        self._et = ExifToolHelper()
        self._et.run()
        # Any failure after the daemon starts must terminate it — __exit__ never
        # runs for a `with` block whose __enter__ raised.
        try:
            self._check_version()
        except BaseException:
            self._et.terminate()
            self._et = None
            raise
        return self

    def __exit__(self, *exc) -> bool:
        if self._et is not None:
            self._et.terminate()
            self._et = None
        return False

    def _check_version(self) -> None:
        assert self._et is not None
        match = re.match(r"(\d+)\.(\d+)", str(self._et.version))
        if match is None:
            raise ExifToolVersionError(f"unparseable ExifTool version: {self._et.version!r}")
        version = (int(match.group(1)), int(match.group(2)))
        if version < self._min_version:
            raise ExifToolVersionError(
                f"ExifTool {version[0]}.{version[1]} < required "
                f"{self._min_version[0]}.{self._min_version[1]} (CVE-2021-22204)"
            )

    def extract(self, path: str | Path) -> MediaMetadata:
        """Extract :class:`MediaMetadata` for a single photo or video."""
        if self._et is None:
            raise RuntimeError("MetadataExtractor used outside its context manager")
        path = Path(path)
        tags = self._et.get_tags([str(path)], tags=_TAGS, params=["-n"])[0]

        mime = str(tags.get("File:MIMEType", ""))
        media_type = "video" if mime.startswith("video/") else "photo"

        exif_gps = None
        lat_raw = tags.get("Composite:GPSLatitude")
        lon_raw = tags.get("Composite:GPSLongitude")
        if lat_raw is not None and lon_raw is not None:
            exif_gps = (float(lat_raw), float(lon_raw))

        ts_tag, ts_value = _coalesce(tags, _TS_TAGS)
        _, width = _coalesce(tags, _WIDTH_TAGS)
        _, height = _coalesce(tags, _HEIGHT_TAGS)
        duration = tags.get("QuickTime:Duration")

        codec = codec_raw = None
        if media_type == "video":
            codec, codec_raw = detect_codec(tags.get("QuickTime:CompressorID"), path)

        # SRT fallback for videos: parse the sidecar telemetry and keep it for
        # audit even when EXIF GPS also exists.
        srt_gps = None
        srt_source = "none"
        if media_type == "video":
            sidecar = _find_srt(path)
            if sidecar is not None:
                srt = parse_srt(sidecar)
                srt_source = srt.gps_source
                if srt.gps_source == "srt":
                    srt_gps = (srt.lat, srt.lon)

        if exif_gps is not None:
            lat, lon, gps_source = exif_gps[0], exif_gps[1], "exif"
        elif srt_gps is not None:
            lat, lon, gps_source = srt_gps[0], srt_gps[1], "srt"
        elif srt_source == "srt_partial":
            lat, lon, gps_source = None, None, "srt_partial"
        else:
            lat, lon, gps_source = None, None, "none"

        return MediaMetadata(
            media_type=media_type,
            lat=lat,
            lon=lon,
            gps_source=gps_source,
            capture_ts_raw=str(ts_value) if ts_value is not None else None,
            capture_ts_source_tag=ts_tag,
            width=int(width) if width is not None else None,
            height=int(height) if height is not None else None,
            duration_s=float(duration) if duration is not None else None,
            codec=codec,
            codec_raw=codec_raw,
            exif_gps=exif_gps,
            srt_gps=srt_gps,
        )
