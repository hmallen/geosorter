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
