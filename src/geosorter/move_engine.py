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

import errno
import hashlib
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import pathing


def sha256_file(
    path: str | Path, *, chunk: int = 1 << 20, on_bytes: Callable[[int], None] | None = None
) -> str:
    """Return the SHA-256 hex digest of a file, read in streaming chunks.

    ``on_bytes``, if given, is called after each chunk with the cumulative number
    of bytes read so far — a progress hook for the slow-network case where hashing
    a multi-GB file is itself a long operation.
    """
    h = hashlib.sha256()
    copied = 0
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
            copied += len(block)
            if on_bytes is not None:
                on_bytes(copied)
    return h.hexdigest()


def _copy_file(
    src: str | Path, dst: str, *, chunk: int = 1 << 20, on_bytes: Callable[[int], None] | None = None
) -> None:
    """Stream-copy ``src`` → ``dst`` in chunks, reporting cumulative bytes.

    Replaces ``shutil.copyfile`` so a long copy over a slow drive can surface live
    progress via ``on_bytes`` (called after each chunk with bytes written so far).
    """
    copied = 0
    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        for block in iter(lambda: fsrc.read(chunk), b""):
            fdst.write(block)
            copied += len(block)
            if on_bytes is not None:
                on_bytes(copied)


@dataclass(frozen=True)
class MoveOutcome:
    """Result of one :func:`copy_and_verify` call."""

    status: str  # 'copy_verified' | 'source_deleted' | 'failed' | 'skipped'
    source_sha256: str | None
    dest_sha256: str | None
    dest_path: str
    error: str | None = None


# Drop the Windows long-path prefix for ``os.path`` string work. The os/shutil
# calls accept the prefixed form directly, but ``os.path`` manipulation is
# clearer without it; the prefixed string is what we store. Shared UNC-aware
# implementation lives in pathing.
_strip_prefix = pathing.strip_long_prefix


def is_already_moved(conn: sqlite3.Connection, source_path: str | Path) -> bool:
    """True if a prior run already fully moved (and deleted) this source path."""
    row = conn.execute(
        "SELECT 1 FROM moves WHERE source_path=? AND status='source_deleted' LIMIT 1",
        (str(source_path),),
    ).fetchone()
    return row is not None


def copy_and_verify(
    conn: sqlite3.Connection,
    batch_id: str,
    source_path: str | Path,
    dest_path: str,
    *,
    source_sha256: str | None = None,
    progress: Callable[[str, int, int], None] | None = None,
    retry_attempts: int = 1,
    retry_backoff_s: float = 0.0,
) -> MoveOutcome:
    """Copy ``source_path`` → ``dest_path`` and verify by SHA-256.

    Leaves the ``moves`` row at ``copy_verified`` on success (the source is not
    deleted — call :func:`commit_delete` for that). Idempotent: a re-run resumes
    from an existing ``pending``/``copy_verified`` row without re-copying a
    byte-verified destination. Returns a :class:`MoveOutcome`.

    ``source_sha256``, if given, is an already-computed digest of the source (e.g.
    the caller's dedup hash) and is used verbatim as the source hash, **skipping the
    redundant re-read** that hashing the source here would cost over a slow share.
    The caller must pass a digest of the source's *current* bytes: on the copy path a
    stale/wrong digest is caught by the destination read-back (the ``.partial`` is
    re-hashed and compared before ``os.replace``, so a mismatch aborts the move), but
    the idempotent-resume short-circuit trusts a digest that matches an existing
    ``copy_verified``/``source_deleted`` ``moves`` row *without* re-reading the source.
    ``organize`` threads its just-computed dedup hash, so this invariant holds. When
    ``None`` the source is hashed here exactly as before; the dest read-back (the
    integrity backstop) is unchanged either way.

    ``progress``, if given, is called as ``progress(phase, done, total)`` during the
    byte-heavy steps (``phase`` ∈ ``'hashing'``/``'copying'``/``'verifying'``,
    ``total`` = the source size) so a caller can show live progress on a slow drive.
    The ``'hashing'`` phase is skipped when ``source_sha256`` is supplied.

    ``retry_attempts``/``retry_backoff_s`` harden the copy against a transient
    ``OSError`` (e.g. an SMB disconnect mid-upload): the copy+verify-hash step is
    retried up to ``retry_attempts`` times with exponential backoff before the move
    is marked ``failed``. ``ENOSPC`` is never retried (disk-full is not transient —
    the caller's free-space recheck owns that). The default ``retry_attempts=1`` is a
    single try, i.e. exactly today's behaviour for every other caller.
    """
    source_path = Path(source_path)
    total = source_path.stat().st_size

    def _emit(phase: str) -> Callable[[int], None] | None:
        return (lambda done: progress(phase, done, total)) if progress is not None else None

    src_sha = (
        source_sha256
        if source_sha256 is not None
        else sha256_file(source_path, on_bytes=_emit("hashing"))
    )

    # SAFETY (recycled-filename collision): refuse to file onto a destination already
    # claimed by a DIFFERENT source — checked on EVERY path (a fresh source AND a
    # stale-row 'pending'/'failed' retry), BEFORE the resume branch below, so a redo can
    # never os.replace over another capture's bytes. A same-source resume matches its OWN
    # row and is excluded by source_path<>?, so legitimate resumes are never blocked.
    occupied = conn.execute(
        "SELECT source_path FROM moves WHERE dest_path=? AND source_path<>? "
        "AND status IN ('copy_verified','source_deleted') LIMIT 1",
        (dest_path, str(source_path)),
    ).fetchone()
    if occupied is not None:
        return MoveOutcome(
            "failed", src_sha, None, dest_path,
            f"destination already filed from a different source: {occupied[0]}",
        )

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
    attempt = 0
    while True:
        try:
            os.makedirs(os.path.dirname(final), exist_ok=True)
            _copy_file(str(source_path), partial, on_bytes=_emit("copying"))
            dest_sha = sha256_file(partial, on_bytes=_emit("verifying"))
            break
        except OSError as err:
            _cleanup(partial)  # never leave an untrusted partial behind
            attempt += 1
            # ENOSPC cannot be retried away; any other OSError (an SMB blip, a
            # transient read/write error) is retried with exponential backoff.
            if err.errno == errno.ENOSPC or attempt >= retry_attempts:
                conn.execute(
                    "UPDATE moves SET status='failed', completed_at=datetime('now') "
                    "WHERE source_path=? AND source_sha256=?",
                    (str(source_path), src_sha),
                )
                conn.commit()
                return MoveOutcome("failed", src_sha, None, dest_path, str(err))
            time.sleep(retry_backoff_s * 2 ** (attempt - 1))

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


def record_pending(
    conn: sqlite3.Connection,
    batch_id: str,
    source_path: str | Path,
    dest_path: str,
    source_sha256: str,
) -> None:
    """Insert a ``pending`` ``moves`` row for a same-volume rename WITHOUT moving.

    Lets the rename path record the whole group in the move log — and persist its
    ``files``/``file_companions`` rows — while every source is still in the inbox, so a
    source is never removed before the index durably knows its destination. This is the
    rename path's equivalent of the copy path's Phase A preceding the `_persist`/delete:
    a crash can never leave the primary's ``source_deleted`` sentinel without a durable
    ``files`` row (which would orphan an unindexed file in the library). Idempotent: a
    no-op if a row for ``(source_path, source_sha256)`` already exists.
    """
    existing = conn.execute(
        "SELECT 1 FROM moves WHERE source_path=? AND source_sha256=?",
        (str(source_path), source_sha256),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO moves(batch_id, source_path, dest_path, source_sha256, "
            "status, started_at) VALUES (?,?,?,?, 'pending', datetime('now'))",
            (batch_id, str(source_path), dest_path, source_sha256),
        )
        conn.commit()


def rename_in_place(
    conn: sqlite3.Connection,
    batch_id: str,
    source_path: str | Path,
    dest_path: str,
    *,
    source_sha256: str | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> MoveOutcome:
    r"""Move ``source_path`` → ``dest_path`` with an atomic same-volume rename.

    The same-volume counterpart to :func:`copy_and_verify` + :func:`commit_delete`:
    when the inbox and library share a volume an ``os.replace`` is a server-side
    rename that moves **zero bytes** and is atomic, so there is nothing in transit to
    corrupt and no read-back verify is needed. The single ``moves`` row goes straight
    to ``source_deleted`` (the source no longer lives at ``source_path``), with
    ``dest_sha256`` set equal to the source hash since the bytes are unchanged — so
    ``undo`` and ``verify-library`` keep working exactly as for a copied file. The
    caller must only invoke this when the two paths are genuinely on one volume
    (``organize._same_volume``); a wrong guess surfaces as an ``EXDEV`` ``OSError``
    which is caught and returned as a ``failed`` outcome with the source left in place
    (no data loss).

    ``source_sha256``, if given (the caller's dedup hash for the primary), is used
    verbatim and the source is **not** re-read. When ``None`` the source is hashed
    here (one read — the only network read on this path), except on crash recovery
    where the source is already gone: the hash is then recovered from the existing
    ``pending`` ``moves`` row (or, last resort, the destination).

    Idempotent by disk state + the ``moves`` log:

    * source present, no row → INSERT ``pending`` (carrying the hash, committed
      *before* the rename so a crash mid-finalize can recover), ``os.replace``, then
      flip to ``source_deleted``;
    * source present, row already ``source_deleted`` → ``skipped``;
    * source gone + dest present → a prior run already renamed it: finalize the row to
      ``source_deleted`` (``skipped`` if it already was) without touching disk;
    * source gone + dest gone → ``failed`` (nothing to move).
    """
    src = Path(source_path)
    final = _strip_prefix(dest_path)

    def _emit(phase: str, total: int) -> Callable[[int], None] | None:
        return (lambda done: progress(phase, done, total)) if progress is not None else None

    # --- Crash-recovery / resume: the source is already gone. ---
    if not src.exists():
        if not os.path.exists(final):
            return MoveOutcome("failed", None, None, dest_path, "source and dest both missing")
        row = conn.execute(
            "SELECT status, source_sha256, dest_sha256 FROM moves WHERE source_path=? "
            "ORDER BY id DESC LIMIT 1",
            (str(src),),
        ).fetchone()
        if row is not None and row[0] == "source_deleted":
            return MoveOutcome("skipped", row[1], row[2], dest_path)
        # The rename already happened (crash before the row was flipped). Recover the
        # hash from the row, the caller, or the destination — never the absent source.
        sha = source_sha256 or (row[1] if row is not None else None) or sha256_file(final)
        if row is not None:
            conn.execute(
                "UPDATE moves SET status='source_deleted', source_sha256=?, dest_sha256=?, "
                "completed_at=datetime('now') WHERE source_path=? AND source_sha256=?",
                (sha, sha, str(src), row[1]),
            )
        else:
            conn.execute(
                "INSERT INTO moves(batch_id, source_path, dest_path, source_sha256, "
                "dest_sha256, status, started_at, completed_at) "
                "VALUES (?,?,?,?,?, 'source_deleted', datetime('now'), datetime('now'))",
                (batch_id, str(src), dest_path, sha, sha),
            )
        conn.commit()
        return MoveOutcome("source_deleted", sha, sha, dest_path)

    # --- Normal path: the source is present. ---
    total = src.stat().st_size
    src_sha = (
        source_sha256
        if source_sha256 is not None
        else sha256_file(src, on_bytes=_emit("hashing", total))
    )

    # SAFETY (recycled-filename collision): refuse to rename onto a destination already
    # claimed by a DIFFERENT source — checked on EVERY path (a fresh source AND a
    # stale-row retry, incl. the same-volume organize flow that record_pending-s the row
    # before this call), BEFORE the resume branch, so a redo can never os.replace over
    # another capture's bytes. A same-source resume is excluded via source_path<>?.
    occupied = conn.execute(
        "SELECT source_path FROM moves WHERE dest_path=? AND source_path<>? "
        "AND status IN ('copy_verified','source_deleted') LIMIT 1",
        (dest_path, str(src)),
    ).fetchone()
    if occupied is not None:
        return MoveOutcome(
            "failed", src_sha, None, dest_path,
            f"destination already filed from a different source: {occupied[0]}",
        )

    existing = conn.execute(
        "SELECT status FROM moves WHERE source_path=? AND source_sha256=?",
        (str(src), src_sha),
    ).fetchone()
    if existing is not None:
        if existing[0] == "source_deleted":
            return MoveOutcome("skipped", src_sha, src_sha, dest_path)
        # 'pending'/'failed'/'copy_verified' → redo the rename (source is still here).
    else:
        conn.execute(
            "INSERT INTO moves(batch_id, source_path, dest_path, source_sha256, "
            "status, started_at) VALUES (?,?,?,?, 'pending', datetime('now'))",
            (batch_id, str(src), dest_path, src_sha),
        )
        conn.commit()

    try:
        os.makedirs(os.path.dirname(final), exist_ok=True)
        os.replace(src, final)
    except OSError as err:
        conn.execute(
            "UPDATE moves SET status='failed', completed_at=datetime('now') "
            "WHERE source_path=? AND source_sha256=?",
            (str(src), src_sha),
        )
        conn.commit()
        return MoveOutcome("failed", src_sha, None, dest_path, str(err))

    conn.execute(
        "UPDATE moves SET status='source_deleted', dest_sha256=?, completed_at=datetime('now') "
        "WHERE source_path=? AND source_sha256=?",
        (src_sha, str(src), src_sha),
    )
    conn.commit()
    return MoveOutcome("source_deleted", src_sha, src_sha, dest_path)


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
