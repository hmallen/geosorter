r"""Crash-safe, idempotent move engine.

The one irreversible action in geosorter is deleting a source after it has been
copied into the library (auto-delete, decision D14). This module makes that
survivable: every file goes through copy → verify-by-hash → (only then) delete,
each transition logged in the ``moves`` table so a crash at any point recovers
cleanly on re-run.

Two primitives, deliberately split so the orchestrator can enforce **group-atomic**
deletes (verify every file in a capture group before deleting any source):

* :func:`copy_and_verify` — recompute the source SHA-256, copy to a ``.partial``
  staging file, hash the copy, and (on match) atomically ``os.replace`` it into
  place, leaving the ``moves`` row at ``copy_verified``. The source is **not**
  touched here.
* :func:`commit_delete` — delete the source and flip the row to ``source_deleted``.

Idempotency keys on ``moves.UNIQUE(source_path, source_sha256)``: a re-run finds
the existing row and resumes from wherever it left off without double-copying or
double-deleting. ``library_root`` may be a different volume (NAS), so this is
always copy-then-delete — never ``os.rename`` (which fails cross-volume and skips
the verify that is the whole safety story).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path


def sha256_file(path: str | Path, *, chunk: int = 1 << 20) -> str:
    """Return the SHA-256 hex digest of a file, read in streaming chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


@dataclass(frozen=True)
class MoveOutcome:
    """Result of one :func:`copy_and_verify` call."""

    status: str  # 'copy_verified' | 'failed' | 'skipped'
    source_sha256: str | None
    dest_sha256: str | None
    dest_path: str
    error: str | None = None


def _strip_prefix(dest_path: str) -> str:
    r"""Drop the Windows ``\\?\`` long-path prefix for ``os.path`` string work.

    The os/shutil calls accept the prefixed form directly, but ``os.path``
    manipulation is clearer without it; the prefixed string is what we store.
    """
    return dest_path[4:] if dest_path.startswith("\\\\?\\") else dest_path


def is_already_moved(conn: sqlite3.Connection, source_path: str | Path) -> bool:
    """True if a prior run already fully moved (and deleted) this source path."""
    row = conn.execute(
        "SELECT 1 FROM moves WHERE source_path=? AND status='source_deleted' LIMIT 1",
        (str(source_path),),
    ).fetchone()
    return row is not None


def copy_and_verify(
    conn: sqlite3.Connection, batch_id: str, source_path: str | Path, dest_path: str
) -> MoveOutcome:
    """Copy ``source_path`` → ``dest_path`` and verify by SHA-256.

    Leaves the ``moves`` row at ``copy_verified`` on success (the source is not
    deleted — call :func:`commit_delete` for that). Idempotent: a re-run resumes
    from an existing ``pending``/``copy_verified`` row without re-copying a
    byte-verified destination. Returns a :class:`MoveOutcome`.
    """
    source_path = Path(source_path)
    src_sha = sha256_file(source_path)

    existing = conn.execute(
        "SELECT status, dest_sha256 FROM moves WHERE source_path=? AND source_sha256=?",
        (str(source_path), src_sha),
    ).fetchone()
    if existing is not None:
        status, dest_sha = existing
        if status == "source_deleted":
            return MoveOutcome("skipped", src_sha, dest_sha, dest_path)
        if status == "copy_verified":
            # Crash after verify, before delete: destination already trusted.
            return MoveOutcome("copy_verified", src_sha, dest_sha, dest_path)
        # 'pending'/'failed' → stale attempt; partial is untrusted, redo the copy.
    else:
        conn.execute(
            "INSERT INTO moves(batch_id, source_path, dest_path, source_sha256, "
            "status, started_at) VALUES (?,?,?,?, 'pending', datetime('now'))",
            (batch_id, str(source_path), dest_path, src_sha),
        )
        conn.commit()

    final = _strip_prefix(dest_path)
    partial = final + ".partial"
    try:
        os.makedirs(os.path.dirname(final), exist_ok=True)
        shutil.copyfile(str(source_path), partial)
        dest_sha = sha256_file(partial)
    except OSError as err:
        _cleanup(partial)
        conn.execute(
            "UPDATE moves SET status='failed', completed_at=datetime('now') "
            "WHERE source_path=? AND source_sha256=?",
            (str(source_path), src_sha),
        )
        conn.commit()
        return MoveOutcome("failed", src_sha, None, dest_path, str(err))

    if dest_sha != src_sha:
        _cleanup(partial)
        conn.execute(
            "UPDATE moves SET status='failed', dest_sha256=?, completed_at=datetime('now') "
            "WHERE source_path=? AND source_sha256=?",
            (dest_sha, str(source_path), src_sha),
        )
        conn.commit()
        return MoveOutcome("failed", src_sha, dest_sha, dest_path, "verify mismatch")

    os.replace(partial, final)
    conn.execute(
        "UPDATE moves SET status='copy_verified', dest_sha256=?, completed_at=datetime('now') "
        "WHERE source_path=? AND source_sha256=?",
        (dest_sha, str(source_path), src_sha),
    )
    conn.commit()
    return MoveOutcome("copy_verified", src_sha, dest_sha, dest_path)


def commit_delete(
    conn: sqlite3.Connection, source_path: str | Path, source_sha256: str
) -> None:
    """Delete the source and flip its ``moves`` row to ``source_deleted``.

    Idempotent: a no-op if the source is already gone. Only call after
    :func:`copy_and_verify` returned ``copy_verified`` for this source.
    """
    source_path = Path(source_path)
    if source_path.exists():
        os.remove(source_path)
    conn.execute(
        "UPDATE moves SET status='source_deleted', completed_at=datetime('now') "
        "WHERE source_path=? AND source_sha256=?",
        (str(source_path), source_sha256),
    )
    conn.commit()


def _cleanup(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
