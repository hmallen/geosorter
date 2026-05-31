"""Companion grouping: attach DJI sidecars to their primary capture.

DJI writes a primary media file plus optional companions sharing the same base
name and counter: ``.DNG`` raw (photos), ``.LRF`` low-res proxy and ``.SRT``
telemetry (videos). A long video also splits into ``_N`` continuation segments
(``DJI_0003.MP4``, ``DJI_0003_001.MP4``...). Grouping happens **before** any
rename, keyed on the original DJI base name.

Two rules, per the task spec:

* ``.DNG``/``.LRF``/``.SRT`` sidecars attach only when their mtime is within
  ``mtime_window_s`` of the primary (default 10 s) — guards against unrelated
  files that happen to share a recycled counter.
* ``_N`` continuation segments attach by **name** regardless of mtime (a later
  segment of a long recording can be minutes apart).

Output feeds B4, which persists ``file_companions`` rows once the primary's
``files`` row exists.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

_DJI_RE = re.compile(r"^(?P<base>DJI_\d+)(?:_(?P<seg>\d+))?$", re.IGNORECASE)
_COMPANION_EXT = {".dng": "dng", ".lrf": "lrf", ".srt": "srt"}
_PRIMARY_EXT = {".jpg", ".jpeg", ".mp4", ".mov"}


@dataclass(frozen=True)
class CaptureGroup:
    """One logical capture: a primary file plus its companions.

    ``companions`` pairs each companion path with its ``companion_type`` —
    one of ``'dng'``/``'lrf'``/``'srt'``/``'other'`` (the ``file_companions``
    enum). Continuation video segments are ``'other'``.
    """

    primary: Path
    companions: list[tuple[Path, str]]


def _parse(path: Path) -> tuple[str, int] | None:
    """Return ``(BASE, segment)`` for a DJI filename, or ``None`` if not DJI."""
    match = _DJI_RE.match(path.stem)
    if match is None:
        return None
    seg = match.group("seg")
    return match.group("base").upper(), (int(seg) if seg is not None else 0)


def group_companions(
    paths: Iterable[Path], *, mtime_window_s: float = 10.0
) -> list[CaptureGroup]:
    """Group DJI files into capture units, attaching companions to each primary.

    Non-DJI filenames are ignored. A base with no primary-extension file (an
    orphaned sidecar) yields no group.
    """
    buckets: dict[str, list[tuple[Path, int]]] = {}
    for path in paths:
        parsed = _parse(path)
        if parsed is None:
            continue
        base, seg = parsed
        buckets.setdefault(base, []).append((path, seg))

    groups: list[CaptureGroup] = []
    for items in buckets.values():
        primaries = [
            (p, seg) for (p, seg) in items if p.suffix.lower() in _PRIMARY_EXT
        ]
        if not primaries:
            continue
        # Lowest segment is the primary; suffix breaks ties deterministically.
        primaries.sort(key=lambda t: (t[1], t[0].suffix.lower()))
        primary = primaries[0][0]
        primary_mtime = primary.stat().st_mtime

        companions: list[tuple[Path, str]] = []
        for path, _seg in items:
            if path == primary:
                continue
            ext = path.suffix.lower()
            if ext in _PRIMARY_EXT:
                # Continuation segment — group by name regardless of mtime.
                companions.append((path, "other"))
            elif ext in _COMPANION_EXT:
                if abs(path.stat().st_mtime - primary_mtime) <= mtime_window_s:
                    companions.append((path, _COMPANION_EXT[ext]))
            else:
                companions.append((path, "other"))
        groups.append(CaptureGroup(primary=primary, companions=companions))

    return groups
