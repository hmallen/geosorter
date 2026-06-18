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


@dataclass(frozen=True)
class InboxGroup:
    """One DJI capture group awaiting ``organize`` — the unit of import selection.

    ``id`` is the inbox-relative POSIX path of the group's primary file; it is the
    stable key the map UI passes back in ``POST /api/organize`` to import a subset,
    and matches what :func:`geosorter.organize.run_organize` filters on. ``dir`` is
    the inbox-relative POSIX parent directory (``""`` for a root-level capture), used
    by the frontend to assemble the directory tree. ``file_count`` is the primary
    plus its companions (so a hyperlapse render + frames reads as one group of N
    files), and ``capture_kind`` mirrors :class:`grouping.CaptureGroup`.
    """

    id: str
    dir: str
    name: str
    capture_kind: str | None
    file_count: int


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
    paths = grouping.scan_inbox_files(inbox)
    result = grouping.prescan_inbox(paths, inbox_root=inbox)
    return InboxCount(files=len(paths), captures=len(result.groups))


def list_inbox(inbox_path: Path | None) -> list[InboxGroup]:
    """List the inbox's DJI capture groups for the import-selection UI.

    The same scan + :func:`grouping.prescan_inbox` as :func:`count_inbox`, but
    returns one :class:`InboxGroup` per capture group (sorted by ``id``) instead of a
    bare count. Returns ``[]`` when ``inbox_path`` is ``None`` or not an existing
    directory.
    """
    if inbox_path is None:
        return []
    inbox = Path(inbox_path)
    if not inbox.is_dir():
        return []
    paths = grouping.scan_inbox_files(inbox)
    result = grouping.prescan_inbox(paths, inbox_root=inbox)
    groups = [
        InboxGroup(
            id=g.primary.relative_to(inbox).as_posix(),
            dir=(
                g.primary.parent.relative_to(inbox).as_posix()
                if g.primary.parent != inbox
                else ""
            ),
            name=g.primary.name,
            capture_kind=g.capture_kind,
            file_count=1 + len(g.companions),
        )
        for g in result.groups
    ]
    return sorted(groups, key=lambda g: g.id)
