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
    # Proxy pre-warm + cap (m-implement-proxy-prewarm-cap): videos whose HEVC→H.264
    # proxy was generated (or was already cached) this pass — counts real HEVC proxies,
    # not H.264 passthroughs; 0 unless `cfg.warm_proxies`. Mirrors the `warmed` field's
    # "generated-or-already-fresh" convention. `proxy_eviction` is the proxy-tier LRU
    # sweep outcome (None when `cfg.proxy_cache_max_gb` is unset → uncapped).
    proxies_warmed: int = 0
    proxy_eviction: EvictionResult | None = None


def warm_library(cfg, batch_id, *, progress=None, cancel=None) -> WarmResult:
    """Pre-generate thumbnails (photos) + posters (videos) for one organized batch.

    Generates ONLY the local-tier browse assets — thumbnails for photos, poster
    frames for videos — for every ``status='organized'`` row of ``batch_id`` on
    ``cfg.cache_dir``, skipping already-fresh assets (so a re-run is a cheap resume).
    A row whose library file is missing on disk is skipped. Each generation runs
    through :func:`derived._generate`'s shared cap, so the pass yields to foreground
    requests. After generation it evicts the local tier to ``cfg.cache_max_gb``.

    ``progress`` (one-arg, the filename) and ``cancel`` (no-arg predicate, polled
    between files) mirror the other background-job entry points. Previews are never
    warmed. HEVC proxies are warmed ONLY when ``cfg.warm_proxies`` is set (opt-in —
    they are large and slow to transcode); a non-HEVC video is a no-op (``derived.proxy``
    returns the source unchanged). When ``cfg.proxy_cache_max_gb`` is set, the proxy
    tier's ``proxies`` kind is LRU-evicted down to it after generation — enforced
    INDEPENDENT of ``warm_proxies`` so the cap also bounds lazily-generated proxies.
    """
    cache_dir = Path(cfg.cache_dir) if cfg.cache_dir else config.default_cache_dir()
    proxy_cache_dir = config.resolve_proxy_cache_dir(cfg)
    conn = db.connect(cfg.index_db_path, integrity_check=False)
    try:
        rows = conn.execute(
            "SELECT dest_path, media_type, codec FROM files "
            "WHERE batch_id=? AND status='organized' ORDER BY id",
            (batch_id,),
        ).fetchall()
    finally:
        conn.close()

    warmed = 0
    proxies_warmed = 0
    for dest_path, media_type, codec in rows:
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
            if cfg.warm_proxies and media_type == "video":
                # Codec-gated inside derived.proxy: an H.264/unknown source returns
                # unchanged (out == source), only HEVC is actually transcoded.
                out = derived.proxy(proxy_cache_dir, rel_key, source, codec)
                if out != source:
                    proxies_warmed += 1
        except Exception:  # a single bad file must not abort the whole warm pass
            logger.warning("warm: failed to generate for %s", source, exc_info=True)
        if progress is not None:
            progress(source.name)

    eviction = derived.evict_local_cache(cache_dir, cfg.cache_max_gb)
    proxy_eviction = (
        derived.evict_proxy_cache(proxy_cache_dir, cfg.proxy_cache_max_gb)
        if cfg.proxy_cache_max_gb is not None
        else None
    )
    return WarmResult(
        batch_id=batch_id,
        warmed=warmed,
        eviction=eviction,
        proxies_warmed=proxies_warmed,
        proxy_eviction=proxy_eviction,
    )
