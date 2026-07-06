r"""Undo the most recent ``organize`` batch (Phase 2 / B8).

``organize`` files each capture into the library and deletes the inbox source
(auto-delete, D14); every step is logged in the ``moves`` table with the original
``source_path`` and the verified ``source_sha256``. This module reverses the most
recent batch: it moves each library file back to its original inbox path with the
same crash-safe copy -> verify -> delete discipline as the forward
:mod:`geosorter.move_engine` (never ``os.rename`` -- ``library_root`` may be a
different volume), then removes the batch's ``files`` / ``file_companions`` /
``moves`` / ``codec_stats`` rows.

Idempotency comes from **disk state**, not a new ``moves`` status: a re-run after a
crash inspects what is already on disk (is the library copy still there? is the
source already restored with the expected hash?) and resumes from there. A source
path that has since been re-occupied by *different* content is never clobbered --
it is skipped and recorded in :attr:`UndoReport.conflicts`.

Row cleanup keys on each physical file's ``moves`` row; deleting a primary's
``files`` row cascades its ``file_companions`` (FK ``ON DELETE CASCADE``, with
``foreign_keys=ON``). The per-batch ``codec_stats`` tally is dropped once no
``moves`` or ``files`` rows remain for the batch.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import db, move_engine, pathing


@dataclass
class UndoReport:
    """Outcome of one :func:`run_undo` call."""

    batch_id: str | None = None
    restored: int = 0
    conflicts: list[str] = field(default_factory=list)  # inbox paths occupied by different content
    failures: list[str] = field(default_factory=list)   # reverse-copy verify failures (row kept)
    missing: int = 0                                     # library copy gone, cannot reverse
    cancelled: bool = False
    nothing_to_undo: bool = False


# Shared long-path prefix handling (single UNC-aware implementation).
_strip = pathing.strip_long_prefix


def latest_batch_id(index) -> str | None:
    """The most recently inserted ``moves`` batch id, or ``None`` if the log is empty."""
    row = index.execute(
        "SELECT batch_id FROM moves WHERE batch_id IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def run_undo(cfg, *, batch_id=None, progress=None, cancel=None) -> UndoReport:
    """Reverse a batch's moves, restoring each file to its original inbox path.

    Targets the most recent batch when ``batch_id`` is ``None``. ``progress`` is a
    one-arg callback invoked per file (mirrors :func:`geosorter.organize.run_organize`).
    ``cancel`` is a no-arg predicate polled **between** files; when it returns True
    the run stops with ``report.cancelled`` set, leaving the remaining rows in place
    so a later run resumes cleanly.
    """
    index = db.connect(cfg.index_db_path)
    db.init_index_schema(index)
    try:
        target = batch_id or latest_batch_id(index)
        report = UndoReport(batch_id=target)
        if target is None:
            report.nothing_to_undo = True
            return report

        # Primary moves rows are inserted before their companions (forward Phase A
        # order), so id order processes a primary before its companions.
        rows = index.execute(
            "SELECT id, file_id, source_path, dest_path, source_sha256, status "
            "FROM moves WHERE batch_id=? ORDER BY id",
            (target,),
        ).fetchall()

        for row in rows:
            if cancel is not None and cancel():
                report.cancelled = True
                break
            _reverse_one(index, row, report, progress)

        # Drop the per-batch codec tally once nothing of the batch remains on disk
        # or in the index (a clean, complete undo).
        if not report.cancelled:
            remaining_moves = index.execute(
                "SELECT 1 FROM moves WHERE batch_id=? LIMIT 1", (target,)
            ).fetchone()
            remaining_files = index.execute(
                "SELECT 1 FROM files WHERE batch_id=? LIMIT 1", (target,)
            ).fetchone()
            if remaining_moves is None and remaining_files is None:
                index.execute("DELETE FROM codec_stats WHERE batch_id=?", (target,))
                index.commit()
        return report
    finally:
        index.close()


def _reverse_one(index, row, report: UndoReport, progress) -> None:
    """Restore one moved file to its inbox source and drop its index rows."""
    move_id, file_id, source_path, dest_path, source_sha256, status = row
    src = Path(source_path)
    dest = Path(_strip(dest_path))

    if progress is not None:
        progress(f"  {dest.name}")

    # Rows that never reached a verified move (failed/aborted/pending): there is no
    # trustworthy library copy to restore. Clear any leftover staging file + the row.
    if status not in ("source_deleted", "copy_verified"):
        _cleanup(str(dest) + ".partial")
        _drop_rows(index, move_id, file_id)
        return

    # Both 'source_deleted' (source already deleted) and 'copy_verified' (source never
    # deleted -- an aborted forward group) reverse through the same disk-state logic:
    # the bytes actually present on disk, not the logged status, decide what to do, so a
    # crash mid-undo *or* mid-organize recovers identically and the only verified library
    # copy is never deleted before its source is safely in place.
    if not dest.exists():
        # Library copy already gone: fully restored already (resume), lost externally,
        # or the inbox path is now occupied by different content we must not touch.
        if not src.exists():
            report.missing += 1
        elif move_engine.sha256_file(src) == source_sha256:
            _drop_rows(index, move_id, file_id)  # resume: the row was the only leftover
            report.restored += 1
        else:
            report.conflicts.append(str(src))
        return

    if src.exists():
        # Inbox path occupied. Our own already-restored file (crash between the
        # os.replace and the dest delete)? Then finish the cleanup. Otherwise it is
        # different content the user dropped -- never clobber it.
        if move_engine.sha256_file(src) == source_sha256:
            os.remove(dest)
            _drop_rows(index, move_id, file_id)
            report.restored += 1
        else:
            report.conflicts.append(str(src))
        return

    # Reverse move: copy library -> source.partial, verify by hash, atomically replace
    # into the inbox, then delete the library copy. Same discipline as the forward move.
    partial = str(src) + ".partial"
    try:
        os.makedirs(os.path.dirname(str(src)), exist_ok=True)
        shutil.copyfile(str(dest), partial)
        restored_sha = move_engine.sha256_file(partial)
    except OSError as err:
        _cleanup(partial)
        report.failures.append(f"{source_path}: {err}")
        return
    if restored_sha != source_sha256:
        _cleanup(partial)
        report.failures.append(f"{source_path}: verify mismatch")
        return
    os.replace(partial, str(src))
    os.remove(dest)
    _drop_rows(index, move_id, file_id)
    report.restored += 1


def _drop_rows(index, move_id, file_id) -> None:
    """Delete a reversed file's ``moves`` row (and, if primary, its ``files`` row).

    Deleting the ``files`` row cascades ``file_companions``. Committed immediately so
    a crash leaves a consistent prefix of already-undone files.
    """
    index.execute("DELETE FROM moves WHERE id=?", (move_id,))
    if file_id is not None:
        index.execute("DELETE FROM files WHERE id=?", (file_id,))
    index.commit()


def _cleanup(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
