r"""Retroactively re-stitch panorama heroes baked in the wrong projection.

Before ``m-fix-panorama-projection-autodetect``, :func:`geosorter.derived.panorama_stitch`
hard-coded ``pano_modify --projection=2`` (equirectangular). A non-360 (180°/wide/
vertical, "flat") panorama therefore had its **pixels** warped into a 2:1
equirectangular canvas and cached that way. Because the wrong geometry is baked into
the cached hero JPEG, fixing the recorded ``files.stitch_projection`` column alone is
not enough — the Hugin pipeline must be **re-run** so the new HFOV auto-detect picks
the right projection, then the new projection is recorded.

``restitch`` is that reconciler. It selects already-stitched panoramas, re-runs the
pipeline with :func:`derived.panorama_stitch`'s ``force=True`` (which bypasses the
freshness cache and runs cold), and records the freshly-detected
``stitch_projection``. It writes the index DB ONLY in two columns
(``stitch_status``/``stitch_projection``); the cached hero JPEG is the sole on-disk
artifact replaced (regenerable, strictly off the crash-safe move path).

Selection (mirrors the user's decision):

* **default** — ``capture_kind='panorama' AND stitch_status='ok' AND
  stitch_projection IS NULL``. The projection column is written ONLY by the new
  auto-detect code, so a NULL projection on an ``'ok'`` stitch is a precise marker
  that the hero was produced by the old hard-coded-equirectangular path.
* **force_all** — every ``stitch_status='ok'`` panorama (drops the NULL clause).

A forced re-stitch that fails (:class:`derived.StitchFailed`) leaves the existing
row + cached hero untouched and is merely reported — least-destructive for a fix
tool. Previously-``'failed'`` panoramas are out of scope (no hero to fix; a normal
stitch covers those). Hugin absent → nothing is attempted.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import config, db, derived, pathing


@dataclass
class RestitchReport:
    """Outcome of one :func:`run_restitch` call."""

    targets: int = 0          # panoramas selected for re-stitch
    restitched: int = 0       # cold re-stitches that succeeded
    failed: int = 0           # StitchFailed — existing hero + row preserved
    unavailable: bool = False # Hugin not found — nothing attempted
    dry_run: bool = False
    projections: dict[str, int] = field(default_factory=dict)  # detected family -> count
    errors: list[str] = field(default_factory=list)


def _strip(dest_path: str) -> str:
    r"""Drop the Windows ``\\?\`` long-path prefix if present."""
    return dest_path[4:] if dest_path.startswith("\\\\?\\") else dest_path


def run_restitch(
    cfg, *, force_all: bool = False, dry_run: bool = False, progress=None
) -> RestitchReport:
    """Re-stitch already-stitched panoramas through the auto-detecting pipeline.

    Selects panoramas (see the module docstring for the ``force_all`` filter),
    re-runs :func:`derived.panorama_stitch` with ``force=True`` per panorama, and
    records the new ``stitch_projection``. ``progress`` is a one-arg per-panorama
    callback (mirrors :func:`geosorter.rescan.run_rescan`). With ``dry_run`` the
    report carries the target count but nothing is stitched and no DB write happens.
    """
    # integrity_check=True (the db.connect default) matches the other destructive
    # index mutators (undo/retag/rescan): a corrupt index DB refuses the operation.
    index = db.connect(cfg.index_db_path)
    db.init_index_schema(index)
    report = RestitchReport(dry_run=dry_run)
    try:
        sql = (
            "SELECT id, dest_path FROM files "
            "WHERE capture_kind='panorama' AND stitch_status='ok'"
        )
        if not force_all:
            sql += " AND stitch_projection IS NULL"
        sql += " ORDER BY id"
        rows = index.execute(sql).fetchall()
        report.targets = len(rows)

        if dry_run:
            for _file_id, dest_path in rows:
                if progress is not None:
                    progress(f"  {os.path.basename(_strip(dest_path))}")
            return report

        # One up-front Hugin probe: absent → nothing we can do, write nothing.
        if derived.find_hugin(cfg.hugin_bin_dir) is None:
            report.unavailable = True
            return report

        proxy_cache_dir = config.resolve_proxy_cache_dir(cfg)
        for file_id, dest_path in rows:
            primary = _strip(dest_path)
            name = os.path.basename(primary)
            if progress is not None:
                progress(f"  {name}")
            frames = [
                _strip(r[0])
                for r in index.execute(
                    "SELECT dest_path FROM file_companions "
                    "WHERE primary_file_id=? AND companion_type='panorama_frame' "
                    "ORDER BY dest_path",
                    (file_id,),
                )
            ]
            rel_key = pathing.library_rel_key(cfg.library_root, dest_path)
            try:
                result = derived.panorama_stitch(
                    proxy_cache_dir, rel_key, primary, frames,
                    hugin_bin_dir=cfg.hugin_bin_dir,
                    canvas=cfg.stitch_canvas,
                    celeste=cfg.stitch_celeste,
                    optimise_lens=cfg.stitch_optimise_lens,
                    force=True,
                )
            except derived.HuginNotFound:
                # Defensive: the up-front probe normally prevents this (Hugin pulled
                # mid-run). Stop — the rest will fail the same way.
                report.unavailable = True
                break
            except (derived.StitchFailed, OSError) as exc:
                # StitchFailed: the gate/Hugin failure raised before _atomic_write, so
                # the existing row + cached hero are left untouched. OSError: a stale/
                # partial row whose primary or a frame left the library makes
                # panorama_stitch raise FileNotFoundError from its tile stat() — report
                # it per-row and keep going (rescan.py's per-row resilience over the
                # same library), never abort the whole batch.
                report.failed += 1
                report.errors.append(f"{name}: {exc}")
                continue

            # A forced cold run always returns a non-empty projection; the empty-string
            # cache-hit value is unreachable under force=True. Guard anyway (mirrors
            # jobs._run_stitch) so a future change can never write '' into the column.
            if result.projection:
                _record_projection(index, file_id, result.projection)
                report.restitched += 1
                report.projections[result.projection] = (
                    report.projections.get(result.projection, 0) + 1
                )
        return report
    finally:
        index.close()


def _record_projection(index, file_id: int, projection: str) -> None:
    """Write the freshly-detected projection for one panorama; commit per row.

    A forced cold re-stitch always returns a non-empty ``projection``, so this is
    only ever called with the real ``'equirectangular'``/``'flat'`` family. Committed
    immediately so a crash leaves a consistent prefix of corrected captures (mirrors
    :func:`geosorter.rescan._prune`)."""
    index.execute(
        "UPDATE files SET stitch_status='ok', stitch_projection=? "
        "WHERE id=? AND capture_kind='panorama'",
        (projection, file_id),
    )
    index.commit()
