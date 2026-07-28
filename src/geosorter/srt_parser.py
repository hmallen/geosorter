"""DJI ``.SRT`` telemetry parser.

DJI writes per-frame flight telemetry into the subtitle (``.SRT``) sidecar that
accompanies a video. There is **no** off-the-shelf library for the payload: the
``srt`` package parses only the subtitle envelope (index / timing / cue text),
not the DJI-specific GPS fields inside each cue.

Two on-disk format families cover the DJI universe, handled by ordered regex
probes against each cue's text:

* **bracket** — modern models (Mini, Air, Mavic 2/3): ``[latitude: X] [longitude: Y]``
  (tolerant of the spaced-colon ``[latitude : X]`` variant).
* **paren** — older DJI GO OSD (Phantom, early Mavic): ``GPS(lon,lat,alt)`` —
  note the **longitude-first** field order.

:func:`parse_srt` returns the **first valid GPS fix**, skipping DJI's pre-lock
``(0,0)`` null-island frames. If a cue carries GPS tokens but no frame ever
yields a range-valid fix, the result is flagged ``gps_source='srt_partial'``
rather than emitting silently-wrong coordinates. No ``eval``/``exec`` is used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# A signed decimal coordinate (DJI always writes a fractional part).
_NUM = r"[-+]?\d+\.\d+"

# Ordered probe families. Each named group ``lat``/``lon`` pins field semantics,
# so the paren family's longitude-first order cannot be transposed by accident.
# latitude and longitude are kept on the SAME physical line (``[^\n]*?``, no
# DOTALL) so a malformed cue that merged two frames cannot pair frame A's
# latitude with frame B's longitude into a transposed fix.
_BRACKET = re.compile(
    r"\[\s*latitude\s*:\s*(?P<lat>" + _NUM + r")\s*\]"
    r"[^\n]*?"
    r"\[\s*longitude\s*:\s*(?P<lon>" + _NUM + r")\s*\]",
    re.IGNORECASE,
)
_PAREN = re.compile(
    r"\bGPS\s*\(\s*(?P<lon>" + _NUM + r")\s*,\s*(?P<lat>" + _NUM + r")\s*,",
    re.IGNORECASE,
)
_PROBES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("bracket", _BRACKET),
    ("paren", _PAREN),
)

# Best-effort capture frame timestamp (``2024-08-26 18:06:42.493`` or the older
# dotted ``2020.04.15 10:30:00`` form). Not on the subtitle-timing line.
_TS = re.compile(r"\d{4}[-.]\d{2}[-.]\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?")


@dataclass(frozen=True)
class SrtResult:
    """Outcome of parsing a DJI SRT sidecar.

    ``gps_source`` is one of ``'srt'`` (a valid fix was found), ``'srt_partial'``
    (GPS tokens present but never a range-valid, non-null fix), or ``'none'``
    (no GPS tokens at all / unreadable file).
    """

    lat: float | None
    lon: float | None
    gps_source: str
    variant: str | None
    first_fix_ts: str | None
    frames_seen: int


@dataclass(frozen=True)
class SrtTrackSample:
    """One range-valid GPS fix aligned to the video's subtitle clock."""

    time_s: float
    lat: float
    lon: float


def _valid(lat: float, lon: float) -> bool:
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return False
    # DJI emits (0,0) for frames captured before GPS lock — not a real fix.
    return not (lat == 0.0 and lon == 0.0)


def parse_srt(path: str | Path) -> SrtResult:
    """Parse a DJI SRT sidecar and return its first valid GPS fix.

    A missing or unreadable file yields ``gps_source='none'`` (the extractor
    consults SRT sidecars opportunistically), never an exception.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return SrtResult(None, None, "none", None, None, 0)

    # Split on blank lines, tolerating a separator line that carries stray
    # whitespace (some DJI exports / editors leave spaces or tabs on it).
    cues = [c for c in re.split(r"\r?\n[ \t]*\r?\n", text) if c.strip()]
    saw_gps_tokens = False
    for cue in cues:
        for name, probe in _PROBES:
            match = probe.search(cue)
            if match is None:
                continue
            saw_gps_tokens = True
            lat = float(match.group("lat"))
            lon = float(match.group("lon"))
            if _valid(lat, lon):
                ts_match = _TS.search(cue)
                first_fix_ts = ts_match.group(0) if ts_match else None
                return SrtResult(lat, lon, "srt", name, first_fix_ts, len(cues))
            # GPS tokens present but this frame is invalid (null/out-of-range):
            # stop probing this cue and try the next frame.
            break

    if saw_gps_tokens:
        return SrtResult(None, None, "srt_partial", None, None, len(cues))
    return SrtResult(None, None, "none", None, None, len(cues))


_CUE_START = re.compile(
    r"(?m)^\s*(?P<hours>\d+):(?P<minutes>\d{2}):(?P<seconds>\d{2})"
    r"(?P<separator>[,.])(?P<millis>\d{3})\s*-->",
)


def _cue_start_seconds(cue: str) -> float | None:
    """Return an SRT cue's start offset, accepting comma or dot milliseconds."""
    match = _CUE_START.search(cue)
    if match is None:
        return None
    minutes = int(match.group("minutes"))
    seconds = int(match.group("seconds"))
    if minutes >= 60 or seconds >= 60:
        return None
    return (
        int(match.group("hours")) * 3600
        + minutes * 60
        + seconds
        + int(match.group("millis")) / 1000
    )


def parse_srt_track(path: str | Path) -> list[tuple[float, float]]:
    """Return every valid GPS fix in cue order — the video's flight track.

    Each element is ``(lat, lon)``. Invalid and pre-lock null-island frames are
    skipped (a track legitimately starts mid-file once GPS locks). The probe
    family is pinned by the first cue that matches one, so a pathological file
    mixing both formats cannot interleave transposed fixes. Missing/unreadable
    file or no valid fixes -> ``[]`` (never an exception), mirroring
    :func:`parse_srt`'s opportunistic contract.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    cues = [c for c in re.split(r"\r?\n[ \t]*\r?\n", text) if c.strip()]
    track: list[tuple[float, float]] = []
    pinned: re.Pattern[str] | None = None
    for cue in cues:
        if pinned is not None:
            match = pinned.search(cue)
        else:
            match = None
            for _, probe in _PROBES:
                match = probe.search(cue)
                if match is not None:
                    pinned = probe
                    break
        if match is None:
            continue
        lat = float(match.group("lat"))
        lon = float(match.group("lon"))
        if _valid(lat, lon):
            track.append((lat, lon))
    return track


def parse_srt_track_samples(path: str | Path) -> list[SrtTrackSample]:
    """Return valid GPS fixes carrying their SRT cue start offsets.

    Cues with usable GPS but malformed/missing subtitle timing are omitted from
    this timed view; :func:`parse_srt_track` still returns their coordinates for
    the static route. Backward-moving timestamps are skipped because frontend
    interpolation requires a monotonic time axis. Equal timestamps are retained
    and resolved deterministically by the client.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    cues = [c for c in re.split(r"\r?\n[ \t]*\r?\n", text) if c.strip()]
    samples: list[SrtTrackSample] = []
    pinned: re.Pattern[str] | None = None
    for cue in cues:
        if pinned is not None:
            match = pinned.search(cue)
        else:
            match = None
            for _, probe in _PROBES:
                match = probe.search(cue)
                if match is not None:
                    pinned = probe
                    break
        if match is None:
            continue
        lat = float(match.group("lat"))
        lon = float(match.group("lon"))
        if not _valid(lat, lon):
            continue
        time_s = _cue_start_seconds(cue)
        if time_s is None or (samples and time_s < samples[-1].time_s):
            continue
        samples.append(SrtTrackSample(time_s=time_s, lat=lat, lon=lon))
    return samples
