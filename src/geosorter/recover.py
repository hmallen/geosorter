"""Recover files damaged by the recycled-DJI-filename destination collision.

Before the ``fix/recycled-filename-dest-collision`` fix, ``organize`` merged two
unrelated files that shared a recycled DJI counter (from different inbox directories)
into one capture group and filed BOTH to a single destination; the second-copied file
overwrote the first and both inbox sources were deleted. The surviving bytes (the
companion, copied last) now sit in the library MISLABELED under the lost primary's
place/date, and the index ``files`` row still carries the lost primary's metadata.

This is a one-off reconciler. For each collision it:

1. un-files the surviving library file (same-volume rename) to
   ``<inbox>/_recovered_collisions/<original-DJI-name>``,
2. drops the three wrong index rows (the phantom ``files`` row — cascading its bogus
   ``file_companions`` row — and both stale ``moves`` rows for the destination),
3. then re-files every survivor through the now-fixed :func:`organize.run_organize`
   (``selected_primaries`` scoped) so the authoritative new rows come from the tested
   pipeline (a GPS-less survivor routes to ``_no-gps/<capture-date>/`` quarantine).

The 43 unrecoverable captures (whose bytes the collision destroyed) are recorded in a
report written next to the index DB. Index DB + on-disk library only; never re-runs the
crash-safe move log blindly — it consumes the collision ``moves`` rows by design.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import db, metadata, move_engine, organize

_RECOVER_SUBDIR = "_recovered_collisions"
_REPORT_NAME = "recovery-report.txt"


@dataclass(frozen=True)
class LostCapture:
    """A capture whose bytes the collision destroyed (only its index metadata remains)."""

    dest: str
    place_string: str | None
    local_date: str | None


@dataclass(frozen=True)
class Collision:
    """One damaged destination: a survivor file mislabeled under a lost capture's row."""

    dest: str  # the wrong library dest_path (raw, with the \\?\ prefix as stored)
    primary_file_id: int
    orig_name: str  # the survivor's original DJI filename (from the staging moves row)
    place_string: str | None  # the LOST capture's recorded place
    local_date: str | None  # the LOST capture's recorded date


@dataclass
class RecoveryReport:
    """Outcome of :func:`run_recovery`."""

    collisions_found: int = 0
    recovered: int = 0
    lost: list[LostCapture] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    dry_run: bool = False
    report_path: str | None = None


def _strip(dest_path: str) -> str:
    r"""Drop the Windows ``\\?\`` long-path prefix (mirror :func:`organize._strip`)."""
    return organize._strip(dest_path)


def find_collisions(index: sqlite3.Connection) -> list[Collision]:
    """Return one :class:`Collision` per destination targeted by >1 ``moves`` row.

    The collision signature is a ``dest_path`` with more than one ``moves`` row (the two
    distinct sources that were filed onto it). Only destinations whose file still exists
    on disk are returned — the survivor we can recover. The survivor's original DJI name
    comes from the ``file_id``-NULL (companion) ``moves`` row's ``source_path``; the lost
    capture's place/date come from the ``files`` row recorded at the destination.
    """
    dests = [
        row[0]
        for row in index.execute(
            "SELECT dest_path FROM moves GROUP BY dest_path HAVING COUNT(*) > 1"
        ).fetchall()
    ]
    collisions: list[Collision] = []
    for dest in dests:
        if not Path(_strip(dest)).exists():
            continue  # survivor gone (already recovered, or moved by hand) — skip
        frow = index.execute(
            "SELECT id, place_string, local_date FROM files WHERE dest_path=?", (dest,)
        ).fetchone()
        if frow is None:
            continue  # no owning files row to reconcile — not the collision shape
        # Prefer the companion (file_id NULL) source name; fall back to any source.
        crow = index.execute(
            "SELECT source_path FROM moves WHERE dest_path=? AND file_id IS NULL LIMIT 1",
            (dest,),
        ).fetchone()
        if crow is None:
            crow = index.execute(
                "SELECT source_path FROM moves WHERE dest_path=? LIMIT 1", (dest,)
            ).fetchone()
        orig_name = Path(_strip(crow[0])).name
        collisions.append(
            Collision(
                dest=dest,
                primary_file_id=frow[0],
                orig_name=orig_name,
                place_string=frow[1],
                local_date=frow[2],
            )
        )
    return collisions


def _relocate(src: str, dst: Path, *, same_volume: bool) -> None:
    """Move ``src`` -> ``dst`` without logging a ``moves`` row.

    Same-volume is an atomic ``os.replace`` (the inbox and library share a volume in
    every real deployment). The cross-volume fallback is a verified copy + delete so the
    survivor is never lost if the source and destination are on different drives.
    """
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if same_volume:
        os.replace(src, dst)
        return
    src_sha = move_engine.sha256_file(src)
    try:
        move_engine._copy_file(src, str(dst))
        if move_engine.sha256_file(str(dst)) != src_sha:
            raise OSError(f"verify mismatch relocating {src} -> {dst}")
    except OSError:
        # Never leave a partial/unverified copy behind: it would make `target.exists()`
        # skip this survivor on a re-run even though the source was never removed.
        move_engine._cleanup(str(dst))
        raise
    os.remove(src)


def _write_report(cfg, report: RecoveryReport) -> str:
    """Write the human-readable recovery report next to the index DB; return its path."""
    path = Path(cfg.index_db_path).parent / _REPORT_NAME
    lines = [
        "geosorter recovery report — recycled-filename collision",
        f"collisions found: {report.collisions_found}",
        f"survivors recovered: {report.recovered}",
        f"failures: {len(report.failures)}",
        "",
        "UNRECOVERABLE captures (bytes destroyed by the collision):",
    ]
    for lc in report.lost:
        lines.append(f"  {lc.local_date}  {lc.place_string}  <- {_strip(lc.dest)}")
    if report.failures:
        lines.append("")
        lines.append("failures:")
        lines.extend(f"  {f}" for f in report.failures)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def run_recovery(
    cfg,
    *,
    dry_run: bool = False,
    progress=None,
    extractor_factory=metadata.MetadataExtractor,
) -> RecoveryReport:
    """Recover every collision survivor and reconcile the index.

    ``progress``, if given, is a one-arg per-collision callback (the survivor's original
    name). ``extractor_factory`` is threaded into :func:`organize.run_organize` for the
    re-file pass (injectable for tests). With ``dry_run`` no disk/DB change is made and no
    report file is written.

    Run under supervision (it is a one-off, not crash-resumable): each survivor is moved
    to the staging dir BEFORE its wrong index rows are dropped, so a crash in that window
    leaves the survivor safe under ``<inbox>/_recovered_collisions/`` (a later ``organize``
    re-files it) and only stale index rows behind (``rescan`` prunes those). Scope is the
    primary survivor per collision; companion sidecars are out of scope (the incident's
    survivors are standalone clips).
    """
    report = RecoveryReport(dry_run=dry_run)
    selected: set[str] = set()

    index = db.connect(cfg.index_db_path)
    try:
        db.init_index_schema(index)  # tolerate a fresh/uninitialized index (report 0)
        collisions = find_collisions(index)
        report.collisions_found = len(collisions)
        if not collisions:
            return report

        # The staging dir lives UNDER inbox_path so run_organize's recursive rglob
        # re-files the survivors. Create it up front (non-dry-run) so it also exists
        # before the same-volume probe — a not-yet-created inbox would make
        # _same_volume return False and force the slower copy path.
        recover_dir = Path(cfg.inbox_path) / _RECOVER_SUBDIR
        if not dry_run:
            recover_dir.mkdir(parents=True, exist_ok=True)
        same_volume = organize._same_volume(cfg.inbox_path, cfg.library_root)
        staged = 0  # survivors un-filed to the staging dir (later re-filed by run_organize)
        for c in collisions:
            if progress is not None:
                progress(c.orig_name)
            report.lost.append(LostCapture(c.dest, c.place_string, c.local_date))
            if dry_run:
                report.recovered += 1
                continue
            # Unique per-collision staging path: recycled DJI counters mean two survivors
            # can share an original basename, so key the target on the (unique) files-row
            # id to keep distinct collisions from colliding on one recovery path.
            target_dir = recover_dir / str(c.primary_file_id)
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / c.orig_name
            if target.exists():
                report.failures.append(
                    f"{c.orig_name}: recovery target already exists — skipped"
                )
                continue
            try:
                _relocate(_strip(c.dest), target, same_volume=same_volume)
            except OSError as err:
                report.failures.append(f"{c.orig_name}: {err}")
                continue
            # Drop the wrong rows. Order matters: the stale moves rows reference the
            # files row (moves.file_id -> files.id, no cascade), so delete them FIRST,
            # then the phantom files row (which cascades its bogus file_companions row).
            index.execute("DELETE FROM moves WHERE dest_path=?", (c.dest,))
            index.execute("DELETE FROM files WHERE id=?", (c.primary_file_id,))
            index.commit()
            selected.add(target.relative_to(cfg.inbox_path).as_posix())
            staged += 1
    finally:
        index.close()

    # Re-file the staged survivors through the fixed pipeline (GPS-less -> _no-gps
    # quarantine). `recovered` reflects what was ACTUALLY re-filed — a staged survivor
    # the re-file pass fails to file is NOT lost: it stays under the staging dir
    # (inside inbox_path) and a later `organize` run will file it.
    if not dry_run and selected:
        batch = organize.run_organize(
            cfg,
            assume_yes=True,
            selected_primaries=selected,
            extractor_factory=extractor_factory,
        )
        report.recovered = batch.organized + batch.quarantined
        report.failures.extend(batch.failures)
        if report.recovered < staged:
            report.failures.append(
                f"{staged - report.recovered} survivor(s) staged but not re-filed; left "
                f"under {_RECOVER_SUBDIR}/ in the inbox for a follow-up `organize` run"
            )
        if batch.aborted:
            report.failures.append("re-file pass aborted before all survivors were filed")
    if not dry_run:
        report.report_path = _write_report(cfg, report)
    return report
