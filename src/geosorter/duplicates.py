"""Pending-duplicate backlog: the ``duplicates`` table + the shared relocation move.

With ``relocate_duplicates`` off, a detected re-import stays in the inbox and
(before this table) nothing was persisted — every organize run re-hashed and
re-skipped the same files forever, with no way to see or drain that backlog
short of flipping the config. Organize now :func:`record`\\ s each skipped
duplicate here and :func:`prune`\\ s the row when the source is relocated or
successfully imported (a former duplicate becomes importable when its library
match is undone). The map UI lists the backlog and drains it via
:func:`dismiss`, which reuses the exact move-to-``_duplicates/``-and-log
mechanism ``relocate_duplicates=on`` uses — :func:`relocate_group` is the
single implementation, called from both here and :mod:`geosorter.organize`.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import grouping, pathing

# Shared long-path prefix handling (single UNC-aware implementation).
_strip = pathing.strip_long_prefix


def record(conn, *, source_path, sha256, companion_paths, matched_file_id,
           matched_dest_path, batch_id) -> None:
    """Upsert the pending-duplicate row for ``source_path``.

    A re-run re-detects the same inbox file, so the upsert refreshes every field
    (the matched library row / companions may have changed between runs) while
    keeping ``first_seen_at`` — the backlog shows when the duplicate first
    appeared, not when it was last re-scanned.
    """
    conn.execute(
        "INSERT INTO duplicates(source_path, sha256, companion_paths, "
        "matched_file_id, matched_dest_path, batch_id) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(source_path) DO UPDATE SET sha256=excluded.sha256, "
        "companion_paths=excluded.companion_paths, "
        "matched_file_id=excluded.matched_file_id, "
        "matched_dest_path=excluded.matched_dest_path, "
        "batch_id=excluded.batch_id",
        (str(source_path), sha256, json.dumps([str(p) for p in companion_paths]),
         matched_file_id, matched_dest_path, batch_id),
    )


def prune(conn, source_path) -> None:
    """Delete the pending-duplicate row for ``source_path``, if any."""
    conn.execute("DELETE FROM duplicates WHERE source_path=?", (str(source_path),))


def list_rows(conn):
    """Every pending-duplicate row, oldest first (stable review order)."""
    return conn.execute(
        "SELECT id, source_path, sha256, companion_paths, matched_file_id, "
        "matched_dest_path, batch_id, first_seen_at FROM duplicates ORDER BY id"
    ).fetchall()


def relocate_group(inbox: Path, primary: Path, companion_paths, matched_dest: str,
                   batch_id: str) -> None:
    r"""Move a duplicate capture into ``<inbox>/_duplicates/`` and append the log line.

    A duplicate is byte-identical content already verified in ``library_root``, so
    this is a plain move (no copy-verify, never touches the library). Each file
    preserves its inbox-relative subpath (suffixed ``_2``/``_3`` on collision, so
    recycled DJI names across cards never clobber), and one TSV line —
    ``<iso ts>\t<batch_id>\t<incoming primary>\t<matched dest>`` — is appended to
    ``_duplicates/duplicates.log``.

    Companions FIRST, primary LAST — mirroring the organizer's move discipline.
    This path is off the crash-safe move log, so if a move fails midway the primary
    must still be in the inbox for a later attempt to rebuild and retry the group
    from it (a primary moved out first would strand any unmoved companion as an
    orphan). A failed move raises :class:`OSError` naming the failing file; files
    already moved stay moved.
    """
    inbox = Path(inbox)
    dup_root = inbox / grouping.DUPLICATES_DIRNAME
    for src in [*(Path(_strip(str(p))) for p in companion_paths),
                Path(_strip(str(primary)))]:
        if not src.exists():
            continue
        target = _dup_target(dup_root, inbox, src)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(src, target)  # atomic within the inbox volume
        except OSError:
            try:
                shutil.move(str(src), str(target))  # cross-device fallback
            except OSError as exc:
                raise OSError(f"{src}: duplicate relocation failed: {exc}") from exc
    dup_root.mkdir(parents=True, exist_ok=True)
    line = (f"{datetime.now().isoformat(timespec='seconds')}\t{batch_id}\t"
            f"{primary}\t{matched_dest}\n")
    with open(dup_root / "duplicates.log", "a", encoding="utf-8") as fh:
        fh.write(line)


def _dup_target(dup_root: Path, inbox: Path, src: Path) -> Path:
    """Non-clobbering ``_duplicates/`` destination preserving ``src``'s inbox subpath."""
    target = dup_root / src.relative_to(inbox)
    if not target.exists():
        return target
    stem, ext = os.path.splitext(target.name)
    n = 2
    while True:
        candidate = target.with_name(f"{stem}_{n}{ext}")
        if not candidate.exists():
            return candidate
        n += 1


@dataclass
class DismissResult:
    """Outcome of one :func:`dismiss` call."""

    dismissed: int = 0
    skipped: int = 0  # unknown ids (already drained by another dismiss / a re-run)
    failures: list[dict] = field(default_factory=list)  # [{"id", "error"}]


def dismiss(conn, ids, inbox_root) -> DismissResult:
    """Drain pending-duplicate rows: relocate each group, then delete its row.

    Per row: a group entirely gone from disk (no primary, no stored companion)
    is dismissed by just deleting the row — nothing moves, no log line, and
    ``_duplicates/`` is never created. Otherwise the row is first re-confirmed
    against the live index (``files.sha256``): an undo since the last organize
    may have removed the library match, and relocating then would shelve the
    only remaining copy — that row stays put, its files untouched, and the
    failure tells the user to re-run organize. Confirmed groups (whatever
    survives of companions + primary; :func:`relocate_group` skips missing
    files) move into ``<inbox>/_duplicates/`` — the same target/suffix/log
    semantics as ``relocate_duplicates=on`` — with the log line naming the
    match's LIVE ``dest_path`` (a retag may have moved it since the
    ``matched_dest_path`` snapshot; the snapshot is only a fallback). Note
    ``companion_paths`` is likewise a snapshot from the last organize scan: a
    sidecar that arrived after that scan and was never re-scanned is not
    covered (acceptable — an organize re-run refreshes the row). Unknown ids
    are counted as ``skipped`` (mirroring how ``assign_locations`` skips
    non-quarantined ids). A per-item failure is recorded and leaves that row in
    place; the remaining ids still process. Commits once at the end.
    """
    result = DismissResult()
    inbox = Path(inbox_root)
    for dup_id in ids:
        row = conn.execute(
            "SELECT source_path, sha256, companion_paths, matched_dest_path, "
            "batch_id FROM duplicates WHERE id=?",
            (dup_id,),
        ).fetchone()
        if row is None:
            result.skipped += 1
            continue
        source_path, sha256, companion_json, matched_dest, batch_id = row
        primary = Path(_strip(source_path))
        companions = json.loads(companion_json)
        if primary.exists() or any(
            Path(_strip(str(c))).exists() for c in companions
        ):
            live = conn.execute(
                "SELECT dest_path FROM files WHERE sha256=? LIMIT 1", (sha256,)
            ).fetchone()
            if live is None:
                result.failures.append({
                    "id": dup_id,
                    "error": "no longer a duplicate (library match was removed); "
                             "run organize to import it",
                })
                continue
            matched = _strip(live[0]) if live[0] else (matched_dest or "")
            try:
                relocate_group(inbox, primary, companions, matched, batch_id or "")
            except (OSError, ValueError) as exc:
                # ValueError: a stored path not under the inbox root (moved config).
                result.failures.append({"id": dup_id, "error": str(exc)})
                continue
        conn.execute("DELETE FROM duplicates WHERE id=?", (dup_id,))
        result.dismissed += 1
    conn.commit()
    return result
