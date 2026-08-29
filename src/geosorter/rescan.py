r"""Reconcile the index DB with on-disk state (Phase 2).

``organize`` files each capture into ``library_root`` and records it in the index
DB (``files`` + ``file_companions`` + ``moves``). The map UI's ``/api/library``
GeoJSON is served straight from the ``files`` table — it never re-checks disk. So
when a filed capture is moved out of ``library_root`` by hand (e.g. a panorama set
dragged back into the inbox), its rows linger and the map keeps showing a phantom
marker with broken media.

``rescan`` is the reconciler: it walks every ``files`` row and prunes the rows
whose primary ``dest_path`` no longer exists on disk. It mutates the **index DB
only** — it NEVER deletes or moves a file on disk. A capture whose primary left the
library is pruned (its ``files`` row, cascading ``file_companions``; its ``moves``
rows; and the per-batch ``codec_stats`` once the batch empties — the same row
cleanup :mod:`geosorter.undo` performs). Two on-disk inconsistencies are reported,
never acted on:

* **Primary present but a companion missing** — recorded in ``warnings``; the row
  is kept (the capture still largely exists and shows on the map).
* **Primary missing but a companion still on disk** — the stale row is pruned (its
  map marker is broken anyway) and the stranded file path is recorded in
  ``orphaned`` so the user can deal with it; rescan never deletes that file.

Detecting files *added* to the library out of band is out of scope — those still
require a normal ``organize`` run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import db, pathing


@dataclass
class RescanReport:
    """Outcome of one :func:`run_rescan` call."""

    checked: int = 0
    kept: int = 0
    pruned: int = 0
    dry_run: bool = False
    warnings: list[str] = field(default_factory=list)   # primary present, companion missing
    orphaned: list[str] = field(default_factory=list)    # primary missing, companion left on disk


# Shared long-path prefix handling (single UNC-aware implementation).
_strip = pathing.strip_long_prefix


def run_rescan(cfg, *, dry_run: bool = False, progress=None) -> RescanReport:
    """Prune index rows whose library file no longer exists on disk.

    Walks every ``files`` row (status ``organized`` AND ``quarantined``). ``progress``
    is a one-arg callback invoked per file (mirrors :func:`geosorter.undo.run_undo`).
    With ``dry_run`` the report carries the same counts but no DB write happens.
    """
    # integrity_check=True (the db.connect default) matches the other destructive
    # index mutators (undo/retag/organize): a corrupt index DB refuses the prune.
    index = db.connect(cfg.index_db_path)
    db.init_index_schema(index)
    report = RescanReport(dry_run=dry_run)
    try:
        rows = index.execute(
            "SELECT id, dest_path, batch_id FROM files ORDER BY id"
        ).fetchall()
        for file_id, dest_path, batch_id in rows:
            report.checked += 1
            name = os.path.basename(_strip(dest_path))
            if progress is not None:
                progress(f"  {name}")

            companions = [
                r[0]
                for r in index.execute(
                    "SELECT dest_path FROM file_companions WHERE primary_file_id=?",
                    (file_id,),
                )
            ]

            if os.path.exists(_strip(dest_path)):
                report.kept += 1
                for cdest in companions:
                    if not os.path.exists(_strip(cdest)):
                        report.warnings.append(
                            f"{name}: companion missing on disk: {_strip(cdest)}"
                        )
                continue

            # Primary missing -> prune. Report any companion still stranded on disk
            # (never delete it).
            for cdest in companions:
                if os.path.exists(_strip(cdest)):
                    report.orphaned.append(_strip(cdest))

            if dry_run:
                report.pruned += 1
            else:
                _prune(index, file_id, dest_path, companions, batch_id, report)
        return report
    finally:
        index.close()


def _prune(index, file_id, primary_dest, companion_dests, batch_id, report) -> None:
    prune_capture(index, file_id, primary_dest, companion_dests, batch_id)
    report.pruned += 1


def prune_capture(index, file_id, primary_dest, companion_dests, batch_id) -> None:
    """Delete one capture's index rows; drop the batch's codec tally if it empties.

    Removes the group's ``moves`` rows (keyed on the stored ``dest_path`` — the
    primary's plus each companion's) and the ``files`` row (cascading
    ``file_companions`` via the FK ``ON DELETE CASCADE``, with ``foreign_keys=ON``).
    Committed immediately so a crash leaves a consistent prefix of pruned captures.
    Shared with :mod:`geosorter.repair`'s delete-broken flow, so a repair-panel
    delete and a rescan prune leave identical index state.
    """
    dests = [primary_dest, *companion_dests]
    placeholders = ",".join("?" * len(dests))
    index.execute(f"DELETE FROM moves WHERE dest_path IN ({placeholders})", dests)
    index.execute("DELETE FROM files WHERE id=?", (file_id,))
    index.commit()

    if batch_id is not None:
        remaining_moves = index.execute(
            "SELECT 1 FROM moves WHERE batch_id=? LIMIT 1", (batch_id,)
        ).fetchone()
        remaining_files = index.execute(
            "SELECT 1 FROM files WHERE batch_id=? LIMIT 1", (batch_id,)
        ).fetchone()
        if remaining_moves is None and remaining_files is None:
            index.execute("DELETE FROM codec_stats WHERE batch_id=?", (batch_id,))
            index.commit()
