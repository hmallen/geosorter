"""Inbox counter — how much is waiting for the next ``organize`` run (B8).

A cheap scan of ``inbox_path`` for the map UI's "inbox" badge. Reports two numbers:

* ``files`` — every file under the inbox (recursively), i.e. exactly what
  :func:`geosorter.organize.run_organize` scans.
* ``captures`` — the number of capture groups
  (:func:`geosorter.grouping.prescan_inbox`), i.e. what ``organize`` will
  actually process. Non-DJI filenames are ignored by the grouper, so they raise
  ``files`` without raising ``captures`` — an honest "inbox clutter that won't
  organize" signal. A hyperlapse render + its frame directory count as ONE capture
  (the pre-scan links the frames as companions), not 1 + N singles.

Deliberately scan-on-request (no ``watchdog`` observer / push channel): drone
inboxes are small and the frontend polls.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import grouping


@dataclass(frozen=True)
class InboxCount:
    """Count of inbox files and DJI capture groups awaiting ``organize``."""

    files: int
    captures: int


def count_inbox(inbox_path: Path | None) -> InboxCount:
    """Count files + DJI capture groups under ``inbox_path``.

    Returns ``InboxCount(0, 0)`` when ``inbox_path`` is ``None`` or not an existing
    directory (the inbox is optional in config and may not exist yet).
    """
    if inbox_path is None:
        return InboxCount(0, 0)
    inbox = Path(inbox_path)
    if not inbox.is_dir():
        return InboxCount(0, 0)
    paths = [p for p in sorted(inbox.rglob("*")) if p.is_file()]
    result = grouping.prescan_inbox(paths, inbox_root=inbox)
    return InboxCount(files=len(paths), captures=len(result.groups))
