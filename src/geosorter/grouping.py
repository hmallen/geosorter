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
from dataclasses import dataclass, replace
from pathlib import Path

# Two DJI naming conventions:
#   classic — DJI_0003 (counter only), split as DJI_0003_001
#   modern  — DJI_<14-digit-timestamp>_<counter>_<lens>, e.g.
#             DJI_20240825165234_0001_D, split as ..._D_001
# The lens letter (_D/_W/_T/_S) is part of ``base`` so distinct lenses are
# distinct captures. The modern shape is tried first; a plain counter falls back
# to classic. ``seg`` is the optional split-video continuation index.
_DJI_RE = re.compile(
    r"^(?P<base>DJI_(?:\d{14}_\d+_[A-Za-z]+|\d+))(?:_(?P<seg>\d+))?$",
    re.IGNORECASE,
)
_COMPANION_EXT = {".dng": "dng", ".lrf": "lrf", ".srt": "srt"}
_PRIMARY_EXT = {".jpg", ".jpeg", ".mp4", ".mov"}


@dataclass(frozen=True)
class CaptureGroup:
    """One logical capture: a primary file plus its companions.

    ``companions`` pairs each companion path with its ``companion_type`` —
    one of ``'dng'``/``'lrf'``/``'srt'``/``'hyperlapse_frame'``/``'other'`` (the
    ``file_companions`` enum). Continuation video segments are ``'other'``.

    ``capture_kind`` is ``None`` for a normal capture, or ``'hyperlapse'`` when the
    directory pre-scan (:func:`prescan_inbox`) has attached a HYPERLAPSE frame
    directory's frames as ``hyperlapse_frame`` companions of this render.
    """

    primary: Path
    companions: list[tuple[Path, str]]
    capture_kind: str | None = None


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


# ---------------------------------------------------------------------------
# Directory-aware ingest pre-scan (B10)
# ---------------------------------------------------------------------------

# A HYPERLAPSE / PANORAMA frame directory is named ``<seq>_<counter>`` (e.g.
# ``001_0021``); the trailing counter matches the render video's ``_<counter>_``
# token. MISC carries no frame directory (catalog DBs, IDX/THM).
_FRAME_DIR_RE = re.compile(r"^\d+_(?P<counter>\d+)$")

# The 4-digit counter inside a modern render stem (``DJI_<14ts>_<counter>_<lens>``).
_RENDER_COUNTER_RE = re.compile(r"^DJI_\d{14}_(?P<counter>\d+)_[A-Za-z]+", re.IGNORECASE)

# DCIM subdirectories that are NOT flat media. HYPERLAPSE is handled here (B10);
# PANORAMA and MISC are recognized but routed out as ``unclaimed`` (B11/B12).
_SPECIAL_DIRS = {"HYPERLAPSE", "PANORAMA", "MISC"}
_FRAME_DIRS = {"HYPERLAPSE", "PANORAMA"}

# The render is written at the END of a hyperlapse capture — minutes after its
# frames (encode time), so the frame↔render mtime guard is FAR more generous than
# the 10 s sidecar window. Its job is only to reject a recycled counter from a
# different session (whose frames sit hours/days away), not to bound encode time.
_HYPERLAPSE_LINK_WINDOW_S = 3600.0


@dataclass(frozen=True)
class PrescanResult:
    """Outcome of :func:`prescan_inbox`.

    ``groups`` are the capture units to organize (flat captures plus hyperlapse
    renders augmented with their frame companions). ``unclaimed`` are recognized
    PANORAMA/MISC paths that B10 does not file (B11/B12 will) — surfaced so the
    caller can warn rather than silently drop them. ``warnings`` are human-readable
    notes for orphan/ambiguous frame directories (a frame dir with no single
    matching render).
    """

    groups: list[CaptureGroup]
    unclaimed: list[Path]
    warnings: list[str]


def _classify(rel_parts: tuple[str, ...]) -> tuple[str, str | None]:
    """Classify one inbox-relative path. Returns ``(kind, counter)``.

    ``kind`` is ``'flat'`` | ``'hyperlapse'`` | ``'panorama'`` | ``'misc'``;
    ``counter`` is the frame-dir counter for HYPERLAPSE/PANORAMA (else ``None``).
    Only directory components are inspected (the trailing filename is ignored).
    """
    dirs = rel_parts[:-1]
    for i, part in enumerate(dirs):
        upper = part.upper()
        if upper in _FRAME_DIRS:
            counter = None
            if i + 1 < len(dirs):
                match = _FRAME_DIR_RE.match(dirs[i + 1])
                counter = match.group("counter") if match else None
            return upper.lower(), counter
        if upper == "MISC":
            return "misc", None
    return "flat", None


def _render_counter(stem: str) -> int | None:
    """The integer counter of a modern render stem, or ``None`` if not modern."""
    match = _RENDER_COUNTER_RE.match(stem)
    return int(match.group("counter")) if match else None


def prescan_inbox(
    paths: Iterable[Path], *, inbox_root: Path, mtime_window_s: float = 10.0
) -> PrescanResult:
    """Partition inbox paths by DCIM subdir, then link hyperlapse frames (B10).

    Flat paths flow through the unchanged :func:`group_companions` fast path. Each
    ``HYPERLAPSE/<seq>_<counter>/`` frame directory is linked to the flat render
    group whose primary stem carries the same ``_<counter>_`` token AND whose mtime
    falls within :data:`_HYPERLAPSE_LINK_WINDOW_S` of the frame mtime span; on a
    unique match the render group becomes ``capture_kind='hyperlapse'`` with the
    frames attached as ``hyperlapse_frame`` companions (sorted by name). A frame dir
    with zero or several matching renders is left unlinked and noted in
    ``warnings`` (never a raise, never a wrong-link). PANORAMA/MISC paths are
    returned in ``unclaimed``.
    """
    inbox_root = Path(inbox_root)
    flat_paths: list[Path] = []
    # frame_dir -> (counter, [frame paths])
    hyperlapse: dict[Path, tuple[str, list[Path]]] = {}
    unclaimed: list[Path] = []
    warnings: list[str] = []

    for path in paths:
        try:
            rel_parts = path.relative_to(inbox_root).parts
        except ValueError:
            flat_paths.append(path)
            continue
        kind, counter = _classify(rel_parts)
        if kind == "flat":
            flat_paths.append(path)
        elif kind == "hyperlapse" and counter is not None:
            hyperlapse.setdefault(path.parent, (counter, []))[1].append(path)
        else:
            # panorama, misc, or a malformed HYPERLAPSE/PANORAMA layout (no
            # frame-dir counter) — recognized, but not filed by B10.
            unclaimed.append(path)

    groups = group_companions(flat_paths, mtime_window_s=mtime_window_s)

    for frame_dir in sorted(hyperlapse):
        counter, frames = hyperlapse[frame_dir]
        frames.sort(key=lambda p: p.name)
        target = int(counter)
        mtimes = [p.stat().st_mtime for p in frames]
        lo = min(mtimes) - _HYPERLAPSE_LINK_WINDOW_S
        hi = max(mtimes) + _HYPERLAPSE_LINK_WINDOW_S
        matched = [
            i
            for i, g in enumerate(groups)
            if g.capture_kind is None
            and _render_counter(g.primary.stem) == target
            and lo <= g.primary.stat().st_mtime <= hi
        ]
        if len(matched) != 1:
            reason = "no matching render" if not matched else "ambiguous render match"
            warnings.append(
                f"hyperlapse frame dir '{frame_dir.name}' (counter {counter}): "
                f"{reason} — {len(frames)} frame(s) left in the inbox"
            )
            continue
        g = groups[matched[0]]
        frame_companions = [(p, "hyperlapse_frame") for p in frames]
        groups[matched[0]] = replace(
            g,
            companions=g.companions + frame_companions,
            capture_kind="hyperlapse",
        )

    return PrescanResult(groups=groups, unclaimed=unclaimed, warnings=warnings)
