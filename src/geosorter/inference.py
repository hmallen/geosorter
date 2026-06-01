"""Time-clustered neighbor-GPS inference (pure, side-effect-free).

DJI captures missing GPS but carrying a capture timestamp can borrow a location
from a *time-adjacent* GPS-bearing capture in the same ``organize`` run. This
module is the pure selection core: given a list of ``(id, timestamp, coord)``
items it assigns each no-coord-but-timestamped item the coordinate of the
nearest-in-time item that *has* a coordinate, but only when the time gap is within
``max_gap``. Out-of-window or timestamp-less items are omitted — the caller routes
those to quarantine (conservative: a missed match never becomes a wrong location).

The caller (``organize``) owns the timezone-normalization policy and supplies the
best comparable instant it can for each item; this module only compares the
``datetime`` objects it is handed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class InferenceResult:
    """One inferred location: ``item_id`` borrowed ``(lat, lon)`` from ``source_id``."""

    item_id: int
    lat: float
    lon: float
    source_id: int


def infer_locations(
    items: list[tuple[int, datetime | None, tuple[float, float] | None]],
    *,
    max_gap: timedelta,
) -> dict[int, InferenceResult]:
    """Infer a coordinate for each no-coord-but-timestamped item.

    ``items`` is a list of ``(id, timestamp, coord)`` where ``coord`` is
    ``(lat, lon)`` or ``None``. For every item that has a timestamp but no coord,
    pick the timestamped item that *does* have a coord and is nearest in time;
    include it only when that gap is ``<= max_gap``. Returns a mapping from the
    inferred item's id to its :class:`InferenceResult`; items left out (no
    timestamp, no candidate, or out of window) are simply absent.
    """
    sources = [
        (item_id, ts, coord)
        for item_id, ts, coord in items
        if ts is not None and coord is not None
    ]
    if not sources:
        return {}

    out: dict[int, InferenceResult] = {}
    for item_id, ts, coord in items:
        if ts is None or coord is not None:
            continue  # no timestamp to cluster on, or already located
        best_id, best_coord, best_gap = None, None, None
        for src_id, src_ts, src_coord in sources:
            gap = abs(ts - src_ts)
            if best_gap is None or gap < best_gap:
                best_id, best_coord, best_gap = src_id, src_coord, gap
        if best_gap is not None and best_gap <= max_gap:
            out[item_id] = InferenceResult(
                item_id=item_id, lat=best_coord[0], lon=best_coord[1], source_id=best_id
            )
    return out
