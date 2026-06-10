"""Post-organize cache warm pass (m-derived-at-scale).

After an ``organize`` batch lands, the first browse of those captures would
otherwise generate every thumbnail/poster on demand under a request storm. This
module pre-generates them on the local cache tier so the first browse is warm,
then evicts the local tier down to ``cache_max_gb``.

It is pure orchestration over the index DB + the :mod:`geosorter.derived`
generators (kept here, not in ``derived``, so ``derived`` stays DB-free — mirroring
:mod:`geosorter.inbox`/:mod:`geosorter.rescan`). It reads the index DB and writes
only cache files; it never touches a library file. Generation goes through
``derived``'s shared concurrency cap, so the warm pass yields to foreground
requests rather than monopolising the CPU.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from . import config, db, derived, pathing
from .derived import EvictionResult

logger = logging.getLogger("geosorter.warm")


def _strip(dest_path: str) -> str:
    """Drop the Windows ``\\\\?\\`` long-path prefix if present."""
    return dest_path[4:] if dest_path.startswith("\\\\?\\") else dest_path


@dataclass
class WarmResult:
    """Outcome of one :func:`warm_library` pass."""

    batch_id: str
    warmed: int  # captures whose thumb/poster was generated or already fresh
    eviction: EvictionResult


def warm_library(cfg, batch_id, *, progress=None, cancel=None) -> WarmResult:
    """Pre-generate thumbnails (photos) + posters (videos) for one organized batch.

    Generates ONLY the local-tier browse assets — thumbnails for photos, poster
    frames for videos — for every ``status='organized'`` row of ``batch_id`` on
    ``cfg.cache_dir``, skipping already-fresh assets (so a re-run is a cheap resume).
    A row whose library file is missing on disk is skipped. Each generation runs
    through :func:`derived._generate`'s shared cap, so the pass yields to foreground
    requests. After generation it evicts the local tier to ``cfg.cache_max_gb``.

    ``progress`` (one-arg, the filename) and ``cancel`` (no-arg predicate, polled
    between files) mirror the other background-job entry points. Previews and HEVC
    proxies are intentionally NOT warmed (large, lazily generated on demand).
    """
    cache_dir = Path(cfg.cache_dir) if cfg.cache_dir else config.default_cache_dir()
    conn = db.connect(cfg.index_db_path, integrity_check=False)
    try:
        rows = conn.execute(
            "SELECT dest_path, media_type FROM files "
            "WHERE batch_id=? AND status='organized' ORDER BY id",
            (batch_id,),
        ).fetchall()
    finally:
        conn.close()

    warmed = 0
    for dest_path, media_type in rows:
        if cancel is not None and cancel():
            break
        source = Path(_strip(dest_path))
        if not source.is_file():  # moved out of the library by hand — nothing to warm
            continue
        rel_key = pathing.library_rel_key(cfg.library_root, dest_path)
        try:
            if media_type == "video":
                derived.poster(cache_dir, rel_key, source)
            else:
                derived.thumbnail(cache_dir, rel_key, source)
            warmed += 1
        except Exception:  # a single bad file must not abort the whole warm pass
            logger.warning("warm: failed to generate for %s", source, exc_info=True)
        if progress is not None:
            progress(source.name)

    eviction = derived.evict_local_cache(cache_dir, cfg.cache_max_gb)
    return WarmResult(batch_id=batch_id, warmed=warmed, eviction=eviction)
