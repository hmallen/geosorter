"""Lazy, cached derived-asset generation for the map viewer (B6).

Thumbnails, video poster frames, and HEVC->H.264 playback proxies are generated
**on first request** and cached under ``<cache_root>/.geosorter-cache/`` so the
crash-safe Phase 0 ``organize`` pipeline never has to depend on Pillow/ffmpeg.

The cache is **tiered** (m-cache-tiering-safety): the caller passes the per-kind
``cache_root`` — thumbs/posters/previews on a local SSD ``cache_dir`` (off the LAN);
proxies/stitch on ``proxy_cache_dir`` (default ``library_root``) — plus a ``rel_key``
(the source's library-relative path, ``pathing.library_rel_key``) so two same-basename
files in different folders get distinct cache files. Freshness is mtime-based: after
generation the cache mtime is ``os.utime``'d to the source's, so a cached file is
reused only while it is at least as new as its source (immune to SMB's coarse mtime
granularity), with no hashing.

ffmpeg/ffprobe are invoked as list-form subprocesses (mirroring
:mod:`geosorter.metadata`); only Pillow is a Python dependency.
"""

from __future__ import annotations

import functools
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISREG

from PIL import Image, ImageOps, UnidentifiedImageError

logger = logging.getLogger("geosorter.derived")

THUMB_MAX = 512
PREVIEW_MAX = 1920
CACHE_DIRNAME = ".geosorter-cache"
# Cap on concurrent Pillow/ffmpeg generation (m-derived-at-scale): a cold-browse
# storm over a 5-20k-file library would otherwise launch one generation per request
# and saturate CPU/SSD. The semaphore is process-wide (single-process uvicorn, per
# the brainstorm) and gates ONLY regeneration — a fresh cache hit never acquires it.
DERIVED_MAX_CONCURRENCY = 4
_GEN_SEMAPHORE = threading.BoundedSemaphore(DERIVED_MAX_CONCURRENCY)
# Local-tier kinds the eviction sweep manages (thumbs/previews/posters/collage live
# on the local SSD cache_dir); proxies/stitch (proxy_cache_dir) are NEVER auto-evicted.
# The panorama collage joins the local tier: it is small, shown on every panorama
# open (hot), and cheap to regenerate — exactly the evictable profile.
_LOCAL_CACHE_KINDS = ("thumbs", "previews", "posters", "collage")
# Instant panorama collage (m-frontend-pano-ux): each tile is downscaled to a
# COLLAGE_CELL-square cell, composed into a near-square grid, then the whole
# collage is capped at COLLAGE_MAX on its long edge.
COLLAGE_CELL = 400
COLLAGE_MAX = 1600
# Hard ceiling on a single proxy transcode (a full HEVC->H.264 of a multi-minute 4K
# clip over SMB is slow, but 30 min only fires on a genuinely stuck process).
FFMPEG_TIMEOUT_S = 1800

# --- Panorama stitch (B13) -------------------------------------------------
# The 360 equirectangular canvas. pano_modify sizes the full sphere to this; a
# 2:1 canvas keeps a longitude:latitude ratio, and --crop=AUTO trims the empty
# margins (so the final long edge is bounded by STITCH_LONG_EDGE_CAP). The default
# shrank from 6000x3000 to 4000x2000 (m-frontend-pano-ux) — ~0.44x the output
# pixels, a meaningful stitch-time cut; overridable via cfg.stitch_canvas.
STITCH_CANVAS = "4000x2000"
STITCH_LONG_EDGE_CAP = 6000
# Output-validity gate: a plausible equirectangular hero is wide, large, and
# (unlike the cv2 failure mode) not a mostly-black void.
STITCH_MIN_LONG_EDGE = 2000
STITCH_MIN_ASPECT = 1.3
STITCH_MAX_ASPECT = 3.0
STITCH_MAX_BLACK_FRAC = 0.15
# Projection auto-detection (m-fix-panorama-projection-autodetect). Not every DJI
# panorama is a full 360 sphere — it also shoots 180/wide/vertical. `autooptimiser -s`
# already estimates the panorama's horizontal field of view and a suitable projection
# into the .pto; we read that HFOV back and pick the pano_modify projection from it
# (instead of the old hard-coded equirectangular), then validate per-projection.
#   - HFOV >= 270deg  -> equirectangular (Hugin code 2), the full-sphere PanoSphere hero
#   - HFOV >= 120deg  -> cylindrical (code 1), a wide "flat" hero
#   - otherwise        -> rectilinear (code 0), a narrow "flat" hero
STITCH_EQUIRECT_MIN_HFOV = 270.0
STITCH_CYLINDRICAL_MIN_HFOV = 120.0
# Hugin pano_modify --projection codes (subset we emit).
_PROJ_RECTILINEAR = 0
_PROJ_CYLINDRICAL = 1
_PROJ_EQUIRECTANGULAR = 2
# A non-equirectangular ("flat") hero is validated against a far wider aspect envelope
# than the equirectangular [1.3, 3.0] — a 180/wide pano is legitimately up to ~6:1, a
# single-row 360 sweep is a wide thin strip (a 360x22 deg sweep is ~16:1), and a vertical
# pano is tall (< 1) — so it is no longer wrongly rejected as "not equirectangular-like".
# The max was raised 8.0 -> 16.0 to admit a single-row 360 strip down to ~22 deg vertical
# FOV (which panorama_stitch reclassifies from equirectangular to flat); the long-edge
# range + black-void guard remain the real degenerate protection for both kinds.
STITCH_FLAT_MIN_ASPECT = 0.2
STITCH_FLAT_MAX_ASPECT = 16.0
# Per-step subprocess ceiling. The spike's slowest step (cpfind) was ~188 s; the
# generous 20 min bound only fires on a genuinely stuck process.
STITCH_STEP_TIMEOUT_S = 1200
# The Hugin CLI tools the stitch pipeline shells out to, in pipeline order.
_HUGIN_TOOLS = (
    "pto_gen",
    "cpfind",
    "cpclean",
    "autooptimiser",
    "pano_modify",
    "hugin_executor",
)


class HuginNotFound(RuntimeError):
    """The Hugin CLI tools were not found on PATH or under ``hugin_bin_dir``."""


class StitchFailed(RuntimeError):
    """A stitch step failed, timed out, or produced a degenerate result."""


@dataclass(frozen=True)
class StitchResult:
    """Outcome of a :func:`panorama_stitch` run.

    ``path`` is the cached hero JPEG. ``projection`` is the viewer-relevant kind the
    pipeline produced — ``'equirectangular'`` (a full-sphere 360, rendered by the
    frontend ``PanoSphere``) or ``'flat'`` (a non-360 pano, rendered as a flat
    zoomable image). On a freshness cache HIT it is ``''`` (empty), meaning "no new
    projection info this call" — the caller keeps whatever was recorded when the hero
    was first stitched (legacy cached heroes predate this feature and are all 360,
    which the frontend treats as equirectangular by default)."""

    path: Path
    projection: str


def _cache_path(cache_root: Path | str, rel_key: str, kind: str, ext: str) -> Path:
    """Cache file under ``cache_root/.geosorter-cache/<kind>/<rel_key>``.

    ``rel_key`` is the source's library-relative POSIX path
    (:func:`geosorter.pathing.library_rel_key`), the same collision-free key media
    URLs use — so two same-basename files in different folders never share a cache
    file, and no ``.resolve()`` (unreliable on a mapped SMB drive, the cause of the
    old wrong-thumbnail collision) is involved. ``cache_root`` is the per-kind tier.
    """
    return (Path(cache_root) / CACHE_DIRNAME / kind / rel_key).with_suffix(ext)


def _is_fresh(cache_file: Path, source: Path) -> bool:
    """True if the cached file exists and is no older than its source."""
    return cache_file.exists() and cache_file.stat().st_mtime >= source.stat().st_mtime


def _touch_to_mtime(cache_file: Path, mtime: float) -> None:
    """Pin a cache file's mtime to ``mtime`` (preserving atime for the future LRU
    sweep) so :func:`_is_fresh` tracks the source exactly — immune to SMB's coarse
    (~2 s) mtime granularity and write-time skew. ``OSError``-tolerant: a disconnected
    share just means a later request may regenerate, never a crash.
    """
    try:
        atime = cache_file.stat().st_atime
        os.utime(cache_file, (atime, mtime))
    except OSError:
        pass


def _touch_to_source(cache_file: Path, source: Path) -> None:
    """Pin the cache mtime to the source's mtime (see :func:`_touch_to_mtime`)."""
    try:
        _touch_to_mtime(cache_file, source.stat().st_mtime)
    except OSError:
        pass


def _run_ffmpeg(cmd: list[str], *, timeout: int = FFMPEG_TIMEOUT_S) -> None:
    """Run an ffmpeg subprocess (list-form) with a hard timeout.

    Maps both a non-zero exit and a timeout to ``RuntimeError``; the partial output is
    cleaned by :func:`_atomic_write` (the caller), which removes its temp on any error.
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ffmpeg timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {proc.stderr.strip()}")


def _atomic_write(out: Path, produce: Callable[[Path], None]) -> None:
    """Build ``out`` via a private temp file, then ``os.replace`` it into place.

    Mirrors :mod:`geosorter.move_engine`'s copy-to-``.partial``-then-replace
    discipline so a concurrent request never observes a half-written cache file:
    each writer produces a unique temp in the same directory and the final swap is
    atomic. Concurrent first-requests may redundantly regenerate, but ``out`` is
    only ever absent or a complete file.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=out.parent, suffix=out.suffix)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        produce(tmp)
        os.replace(tmp, out)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _generate(out: Path, source: Path, produce: Callable[[Path], None]) -> None:
    """Produce ``out`` under the shared generation cap, then pin its mtime to source.

    Acquires :data:`_GEN_SEMAPHORE` (capping concurrent Pillow/ffmpeg work) and
    re-checks freshness *under* the permit, so a request that lost the race to a
    just-finished generator returns the fresh file instead of redoing the work. The
    cache-hit fast path in the callers never reaches here, so a hit stays lock-free.
    """
    with _GEN_SEMAPHORE:
        if _is_fresh(out, source):
            return
        _atomic_write(out, produce)
        _touch_to_source(out, source)


def _resize_jpeg(cache_root: Path | str, rel_key: str, source: Path, kind: str,
                 max_px: int, quality: int) -> Path:
    """Return a cached downscaled JPEG of an image (long edge <= ``max_px``)."""
    out = _cache_path(cache_root, rel_key, kind, ".jpg")
    if _is_fresh(out, source):
        return out

    def _produce(dest: Path) -> None:
        with Image.open(source) as img:
            if source.suffix.lower() in (".jpg", ".jpeg"):
                # DCT-downscale the JPEG decode toward the target (4-8x faster on
                # large DJI JPEGs, fewer SMB bytes read). Must precede any pixel
                # access — exif_transpose/thumbnail below load the image. No-op for
                # non-JPEG, but guard by suffix so the intent is explicit.
                img.draft("RGB", (max_px, max_px))
            img = ImageOps.exif_transpose(img)  # honour camera orientation
            img.thumbnail((max_px, max_px))  # downscale only; never upscales
            img.convert("RGB").save(dest, "JPEG", quality=quality)

    _generate(out, source, _produce)
    return out


def thumbnail(cache_root: Path | str, rel_key: str, source: Path | str) -> Path:
    """Return a cached 512px JPEG thumbnail of an image, generating it if stale."""
    return _resize_jpeg(cache_root, rel_key, Path(source), "thumbs", THUMB_MAX, quality=85)


def preview(cache_root: Path | str, rel_key: str, source: Path | str) -> Path:
    """Return a cached 1080p (1920px long-edge) JPEG preview for the lightbox."""
    return _resize_jpeg(cache_root, rel_key, Path(source), "previews", PREVIEW_MAX, quality=88)


def poster(cache_root: Path | str, rel_key: str, source: Path | str) -> Path:
    """Return a cached JPEG poster frame for a video, generating it if stale."""
    source = Path(source)
    out = _cache_path(cache_root, rel_key, "posters", ".jpg")
    if _is_fresh(out, source):
        return out
    _generate(
        out, source,
        lambda dest: _run_ffmpeg(
            ["ffmpeg", "-v", "error", "-y", "-ss", "0", "-i", str(source),
             "-frames:v", "1", str(dest)]
        ),
    )
    return out


@functools.lru_cache(maxsize=1)
def _detect_nvenc() -> bool:
    """Return True if the ffmpeg on PATH advertises the ``h264_nvenc`` encoder.

    Probes ``ffmpeg -encoders`` once (the answer is fixed for the process's lifetime —
    the ffmpeg build / GPU does not change under us — so the result is memoized). Any
    failure to run ffmpeg (absent binary, OS error, timeout) reports no NVENC, so the
    caller transparently uses the CPU ``libx264`` path. Mirrors :func:`find_hugin`'s
    runtime-detect-then-fall-back discipline.
    """
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "h264_nvenc" in proc.stdout


def _proxy_cmd(source: Path, dest: Path, *, nvenc: bool) -> list[str]:
    """Build the ffmpeg command for an HEVC->H.264 proxy transcode.

    The NVENC path decodes, scales, and encodes entirely on the GPU. The
    ``scale_cuda=format=yuv420p`` filter downconverts a 10-bit Main10 source (common for
    DJI D-Log) to the 8-bit ``yuv420p`` that ``h264_nvenc`` requires; it is a cheap no-op
    on an already-8-bit source, so one command covers both bit depths. ``-cq 23`` is the
    constant-quality (size/quality) knob. The libx264 path is the original CPU command.
    """
    if nvenc:
        return [
            "ffmpeg", "-v", "error", "-y",
            "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
            "-i", str(source),
            "-vf", "scale_cuda=format=yuv420p",
            "-c:v", "h264_nvenc", "-preset", "p5", "-cq", "23",
            "-c:a", "aac", "-movflags", "+faststart", str(dest),
        ]
    return [
        "ffmpeg", "-v", "error", "-y", "-i", str(source),
        "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart", str(dest),
    ]


def proxy(cache_root: Path | str, rel_key: str, source: Path | str, codec: str | None,
          *, hwaccel: str = "auto") -> Path:
    """Return a browser-playable video path for ``source``.

    H.264 (or unknown) sources are already streamable and are returned unchanged.
    HEVC sources are transcoded to a cached H.264 proxy under ``proxies/``.

    ``hwaccel`` (#124) selects the encoder: ``'none'`` always uses CPU ``libx264``;
    ``'nvenc'`` forces GPU NVENC (strict — an encode failure propagates); ``'auto'``
    (default) uses NVENC when :func:`_detect_nvenc` finds it and otherwise libx264, AND
    falls back to libx264 if an NVENC encode fails at runtime (silent correctness on a
    flaky GPU / unsupported source). NVENC is ~5-15x faster on 4K HEVC.
    """
    source = Path(source)
    if codec != "h265":
        return source
    out = _cache_path(cache_root, rel_key, "proxies", ".mp4")
    if _is_fresh(out, source):
        return out

    use_nvenc = hwaccel == "nvenc" or (hwaccel == "auto" and _detect_nvenc())
    allow_fallback = hwaccel == "auto"

    def _produce(dest: Path) -> None:
        if use_nvenc:
            try:
                _run_ffmpeg(_proxy_cmd(source, dest, nvenc=True))
                return
            except RuntimeError:
                if not allow_fallback:
                    raise  # explicit 'nvenc' is strict — surface the failure
                logger.warning(
                    "NVENC proxy transcode failed for %s; falling back to libx264", source
                )
        _run_ffmpeg(_proxy_cmd(source, dest, nvenc=False))

    _generate(out, source, _produce)
    return out


def panorama_collage(
    cache_root: Path | str,
    rel_key: str,
    primary_source: Path | str,
    tile_sources: Sequence[Path | str],
) -> Path:
    """Return a cached raw-tile collage JPEG for a panorama (Pillow only, no Hugin).

    The instant placeholder shown the moment a panorama opens, while the optional
    multi-minute Hugin stitch (:func:`panorama_stitch`) is absent or still running.
    Each tile is downscaled to a :data:`COLLAGE_CELL`-square cell and composed into a
    near-square grid (primary tile first) on a black canvas, then the whole image is
    capped at :data:`COLLAGE_MAX` on its long edge. Cached under
    ``<cache_root>/.geosorter-cache/collage/`` on the LOCAL tier (small, hot,
    regenerable) and mtime-pinned to the newest tile (SMB-safe, like the stitch).

    A single unreadable/corrupt tile leaves its cell black and never aborts the
    collage. Regeneration is gated by the shared :data:`_GEN_SEMAPHORE` (so a
    cold-browse storm cannot fan out unbounded Pillow work); a fresh cache hit
    returns before acquiring it.
    """
    primary_source = Path(primary_source)
    tiles = [primary_source, *(Path(t) for t in tile_sources)]
    out = _cache_path(cache_root, rel_key, "collage", ".jpg")
    # Freshness over the tiles that EXIST on disk: a panorama_frame companion can be
    # gone (rescan keeps that present-primary/missing-companion state as a warning,
    # row retained), and the collage must still serve a degraded (black-cell)
    # placeholder rather than 500 — matching _produce's per-tile tolerance below. A
    # bare max() over a missing tile would raise FileNotFoundError before _produce.
    mtimes: list[float] = []
    for tile in tiles:
        try:
            mtimes.append(tile.stat().st_mtime)
        except OSError:
            continue
    newest = max(mtimes) if mtimes else 0.0
    if out.exists() and out.stat().st_mtime >= newest:
        return out

    cols = math.ceil(math.sqrt(len(tiles)))
    rows = math.ceil(len(tiles) / cols)

    def _produce(dest: Path) -> None:
        canvas = Image.new("RGB", (cols * COLLAGE_CELL, rows * COLLAGE_CELL), (0, 0, 0))
        for idx, tile in enumerate(tiles):
            try:
                with Image.open(tile) as img:
                    if tile.suffix.lower() in (".jpg", ".jpeg"):
                        # DCT-downscale the JPEG decode toward the cell size (faster,
                        # fewer bytes read) — must precede any pixel access. Mirrors
                        # _resize_jpeg; a no-op for non-JPEG tiles.
                        img.draft("RGB", (COLLAGE_CELL, COLLAGE_CELL))
                    img = ImageOps.exif_transpose(img)  # honour camera orientation
                    img.thumbnail((COLLAGE_CELL, COLLAGE_CELL))
                    img = img.convert("RGB")
            except (OSError, UnidentifiedImageError):
                continue  # bad/missing tile → leave its cell black, never abort
            r, c = divmod(idx, cols)
            x = c * COLLAGE_CELL + (COLLAGE_CELL - img.width) // 2
            y = r * COLLAGE_CELL + (COLLAGE_CELL - img.height) // 2
            canvas.paste(img, (x, y))
        if max(canvas.size) > COLLAGE_MAX:
            canvas.thumbnail((COLLAGE_MAX, COLLAGE_MAX))
        canvas.save(dest, "JPEG", quality=82)

    with _GEN_SEMAPHORE:
        # Re-check under the permit: a request that lost the race to a just-finished
        # generator returns the fresh file instead of recomposing.
        if out.exists() and out.stat().st_mtime >= newest:
            return out
        _atomic_write(out, _produce)
        _touch_to_mtime(out, newest)  # pin freshness to the newest tile (SMB-safe)
    return out


@dataclass
class EvictionResult:
    """Outcome of one :func:`evict_local_cache` sweep."""

    bytes_before: int
    bytes_after: int
    deleted: int
    skipped: int  # files whose unlink raised (open on Windows) — left in place


def evict_local_cache(cache_root: Path | str, max_gb: float) -> EvictionResult:
    """Atime-sweep the LOCAL derived tier under ``cache_root`` down to ``max_gb``.

    Walks ``thumbs``/``previews``/``posters``/``collage`` (the local-SSD tier —
    proxies/stitch on ``proxy_cache_dir`` are deliberately NOT swept here; the proxy
    tier has its own :func:`evict_proxy_cache`). Returns the byte totals before/after
    plus the deleted/skipped counts. See :func:`_evict_cache` for the sweep semantics.
    """
    return _evict_cache(cache_root, max_gb, _LOCAL_CACHE_KINDS)


def evict_proxy_cache(cache_root: Path | str, max_gb: float) -> EvictionResult:
    """Atime-sweep the PROXY tier's ``proxies`` kind under ``cache_root`` to ``max_gb``.

    Caps the HEVC→H.264 playback proxies only. The ``stitch`` kind shares this tier but
    is deliberately NOT swept — a panorama hero costs minutes of Hugin to regenerate,
    while a proxy is a cheap, automatic ffmpeg transcode. Otherwise identical to
    :func:`evict_local_cache` (see :func:`_evict_cache`).
    """
    return _evict_cache(cache_root, max_gb, ("proxies",))


def _evict_cache(
    cache_root: Path | str, max_gb: float, kinds: tuple[str, ...]
) -> EvictionResult:
    """Atime-LRU sweep of the given cache ``kinds`` under ``cache_root`` down to ``max_gb``.

    While the total exceeds the cap, deletes files least-recently-accessed first (by
    ``st_atime``). A file whose ``unlink`` raises ``PermissionError``/``OSError`` (open
    on Windows) is skipped and the sweep continues — eviction is best-effort, never
    fatal. Only ever walks ``<cache_root>/.geosorter-cache/<kind>/``, so it can never
    touch library media even when ``cache_root`` equals ``library_root``.

    Note: on Windows with last-access updates disabled, ``st_atime`` tracks roughly
    creation/write time, so the policy degrades to oldest-first — still sound.
    """
    base = Path(cache_root) / CACHE_DIRNAME
    entries: list[tuple[float, int, Path]] = []
    for kind in kinds:
        kind_dir = base / kind
        if not kind_dir.is_dir():
            continue
        for f in kind_dir.rglob("*"):
            # One guarded stat (not is_file()+stat): a concurrent _atomic_write temp
            # in a swept dir can be renamed away between listing and stat, which would
            # otherwise raise FileNotFoundError and abort the whole sweep.
            try:
                st = f.stat()
            except OSError:
                continue
            if S_ISREG(st.st_mode):
                entries.append((st.st_atime, st.st_size, f))

    total = sum(size for _, size, _ in entries)
    before = total
    cap = int(max_gb * (1 << 30))
    deleted = skipped = 0
    if total > cap:
        entries.sort(key=lambda e: e[0])  # least-recently-accessed first
        for _atime, size, f in entries:
            if total <= cap:
                break
            try:
                f.unlink()
            except FileNotFoundError:
                total -= size  # already gone (raced) — that space is genuinely freed
                continue
            except OSError:  # PermissionError (open on Windows) — still occupies space
                skipped += 1
                continue
            total -= size
            deleted += 1
    return EvictionResult(bytes_before=before, bytes_after=total,
                          deleted=deleted, skipped=skipped)


def find_hugin(hugin_bin_dir: Path | str | None = None) -> dict[str, str] | None:
    """Locate the Hugin CLI tools, or ``None`` if any is missing.

    Each tool is resolved with :func:`shutil.which` (which appends the platform's
    executable extensions, so ``.exe`` is found on Windows) on PATH, or under
    ``hugin_bin_dir`` when that optional config key is set. Returns a
    ``name -> resolved path`` dict only when **every** tool in ``_HUGIN_TOOLS`` is
    present; a single missing tool yields ``None`` so the caller falls back to the
    B12 gallery (Hugin is an optional, runtime-detected dependency — no hard import).
    """
    base = Path(hugin_bin_dir) if hugin_bin_dir else None
    tools: dict[str, str] = {}
    for name in _HUGIN_TOOLS:
        target = str(base / name) if base is not None else name
        found = shutil.which(target)
        if found is None:
            return None
        tools[name] = found
    return tools


def stitch_cache_path(cache_root: Path | str, rel_key: str) -> Path:
    """The cache location of a panorama's stitched hero (whether or not it exists).

    Shared by :func:`panorama_stitch` (the writer, via the stitch background job) and
    the ``/api/stitch`` serve route (the reader) so both agree on the path without the
    route reaching into the private cache helper. Both pass the panorama primary's
    ``rel_key`` (``pathing.library_rel_key``) and the ``proxy_cache_dir`` tier, so the
    generator and the reader resolve to the same file.
    """
    return _cache_path(cache_root, rel_key, "stitch", ".jpg")


def _run_hugin(cmd: list[str], *, timeout: int = STITCH_STEP_TIMEOUT_S) -> None:
    """Run one Hugin pipeline step (list-form, never ``shell=True``).

    Mirrors :func:`_run_ffmpeg` but adds a hard ``timeout`` per step and maps both
    a non-zero exit and a timeout to :class:`StitchFailed` so the caller can fall
    back to the gallery.
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise StitchFailed(f"{Path(cmd[0]).name} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise StitchFailed(
            f"{Path(cmd[0]).name} failed ({proc.returncode}): {proc.stderr.strip()}"
        )


def _parse_pto_hfov(pto_text: str) -> float:
    """Read the panorama horizontal field of view from an optimised Hugin ``.pto``.

    ``autooptimiser -s`` writes the output panorama's geometry onto the project's
    ``p`` line, e.g. ``p f2 w6000 h3000 v360 ...`` where ``v`` is the horizontal FOV
    in degrees. We read that ``v`` back to decide the projection instead of forcing
    equirectangular. Raises :class:`StitchFailed` when no ``p`` line / ``v`` token is
    present (a malformed or unoptimised project — treated as a stitch failure)."""
    for line in pto_text.splitlines():
        if line.startswith("p "):
            match = re.search(r"\bv([\d.]+)", line)
            if match is not None:
                try:
                    return float(match.group(1))
                except ValueError as exc:  # a malformed token the loose regex admitted
                    raise StitchFailed(
                        f"malformed panorama HFOV token {match.group(1)!r}"
                    ) from exc
    raise StitchFailed("could not read panorama HFOV from optimised project")


def _choose_projection(hfov: float) -> tuple[int, str]:
    """Map a panorama HFOV to a ``(pano_modify projection code, viewer kind)`` pair.

    A near-full sphere stays equirectangular (the ``PanoSphere`` 360 hero); a narrower
    field of view becomes a ``'flat'`` hero (cylindrical for a wide pano, rectilinear
    for a narrow one) so it is no longer forced into a 2:1 sphere and rejected."""
    if hfov >= STITCH_EQUIRECT_MIN_HFOV:
        return (_PROJ_EQUIRECTANGULAR, "equirectangular")
    if hfov >= STITCH_CYLINDRICAL_MIN_HFOV:
        return (_PROJ_CYLINDRICAL, "flat")
    return (_PROJ_RECTILINEAR, "flat")


# Manual projection overrides offered by the map UI's re-stitch control. The three
# real Hugin projections map to their (pano_modify code, stored viewer family) pair;
# cylindrical and rectilinear both store as the non-360 'flat' family.
_FORCED_PROJECTIONS: dict[str, tuple[int, str]] = {
    "equirectangular": (_PROJ_EQUIRECTANGULAR, "equirectangular"),
    "cylindrical": (_PROJ_CYLINDRICAL, "flat"),
    "rectilinear": (_PROJ_RECTILINEAR, "flat"),
}


def _forced_projection_code(name: str) -> tuple[int, str]:
    """Map a manual projection override name to its ``(Hugin code, family)`` pair.

    Raises :class:`StitchFailed` for an unknown name (a bad override is a stitch
    failure, surfaced like any other so the existing hero/row is preserved)."""
    try:
        return _FORCED_PROJECTIONS[name]
    except KeyError as exc:
        raise StitchFailed(f"unknown forced projection {name!r}") from exc


def classify_stitched_projection(path: Path) -> str:
    """Best-effort projection kind of an ALREADY-stitched hero, from its aspect.

    The cold-run HFOV-derived projection (:func:`_choose_projection`) is authoritative
    and is what gets recorded. This aspect-based fallback exists only to BACKFILL a
    projection that was never recorded — e.g. a cache file that survived an index-DB
    rebuild, or a crash between the cache write and the DB update — so a cached flat
    hero is not stuck defaulting to the 360 sphere viewer. It is never used to
    overwrite a recorded value (aspect cannot distinguish a 2:1 cylindrical pano from a
    true equirectangular, so it is a recovery heuristic, not the source of truth)."""
    with Image.open(path) as img:
        width, height = img.size
    aspect = width / height if height else 0.0
    if STITCH_MIN_ASPECT <= aspect <= STITCH_MAX_ASPECT:
        return "equirectangular"
    return "flat"


def _stitch_gate(path: Path, kind: str = "equirectangular") -> None:
    """Raise :class:`StitchFailed` unless ``path`` is a plausible stitch for ``kind``.

    The cv2 failure mode the spike rejected was a warped partial with a ~45% black
    void. The gate verifies a sane size, an aspect within the envelope for the chosen
    projection (equirectangular is the original ``[1.3, 3.0]``; a non-360 ``'flat'``
    pano uses the far wider ``[0.2, 16.0]`` so a 180/wide/single-row-360/vertical result
    is accepted),
    and a near-black-pixel fraction under :data:`STITCH_MAX_BLACK_FRAC`, so a
    degenerate or failed stitch is never cached or served. The long-edge range and the
    black-void guard are projection-independent and apply to both kinds.
    """
    if kind == "equirectangular":
        min_aspect, max_aspect = STITCH_MIN_ASPECT, STITCH_MAX_ASPECT
    else:
        min_aspect, max_aspect = STITCH_FLAT_MIN_ASPECT, STITCH_FLAT_MAX_ASPECT
    with Image.open(path) as img:
        width, height = img.size
        long_edge = max(width, height)
        if not (STITCH_MIN_LONG_EDGE <= long_edge <= STITCH_LONG_EDGE_CAP):
            raise StitchFailed(f"stitch long-edge {long_edge}px out of range")
        aspect = width / height if height else 0.0
        if not (min_aspect <= aspect <= max_aspect):
            raise StitchFailed(f"stitch aspect {aspect:.2f} out of range for {kind}")
        small = img.convert("L")
        small.thumbnail((512, 512))  # sample cheaply; the void fraction is global
        histogram = small.histogram()  # 256 luminance buckets
        total = small.size[0] * small.size[1]
        black = sum(histogram[:8])  # near-black (luminance < 8) pixels
        frac = black / total if total else 1.0
    if frac > STITCH_MAX_BLACK_FRAC:
        raise StitchFailed(f"stitch is {frac:.0%} black void — degenerate")


def panorama_stitch(
    cache_root: Path | str,
    rel_key: str,
    primary_source: Path | str,
    frame_sources: Sequence[Path | str],
    *,
    hugin_bin_dir: Path | str | None = None,
    canvas: str = STITCH_CANVAS,
    celeste: bool = True,
    optimise_lens: bool = True,
    force: bool = False,
    forced_projection: str | None = None,
    on_step: Callable[[int, int, str], None] | None = None,
) -> StitchResult:
    """Return a cached JPEG hero stitched from a panorama's tiles + its projection.

    Runs the spike-proven Hugin pipeline (``pto_gen -> cpfind --multirow --celeste
    -> cpclean -> autooptimiser -> pano_modify -> hugin_executor``) in a private temp
    dir, each step a list-form subprocess with a timeout, strictly off the crash-safe
    move path (called only by the background stitch job). The result is mtime-cached
    under ``library_root/.geosorter-cache/stitch/`` (fresh while no tile is newer than
    the cache) and validity-gated before caching.

    The projection is auto-detected (m-fix-panorama-projection-autodetect): after
    ``autooptimiser -s`` estimates the panorama geometry, the optimised ``.pto`` HFOV
    is read (:func:`_parse_pto_hfov`) and mapped (:func:`_choose_projection`) to the
    ``pano_modify --projection`` code, so a non-360 (180/wide/vertical) pano stitches
    to a valid ``'flat'`` hero instead of being forced into an equirectangular sphere
    and rejected. The returned :class:`StitchResult` carries that projection kind so
    the caller can record it and the frontend can pick its viewer.

    Each pipeline step is logged (with its elapsed time) and, when ``on_step`` is
    given, reported as ``on_step(step_index, step_total, step_name)`` *before* the
    step runs — so the background job and the map UI can show which of the six steps
    is currently executing during the multi-minute run.

    ``forced_projection`` (default None) overrides the HFOV-derived projection with a
    user-chosen one (``'equirectangular'``/``'cylindrical'``/``'rectilinear'`` via
    :func:`_forced_projection_code`) — the map UI's manual re-stitch control. When set,
    the one-way equirectangular->flat reclassification below is skipped so the explicit
    choice is honoured verbatim.

    ``force`` (default False) skips ONLY the freshness early-return, so a hero baked
    at the old hard-coded projection can be re-stitched cold through the now
    auto-detecting pipeline (the ``restitch`` verb's path). It is safe: ``_stitch_gate``
    and any Hugin step raise BEFORE the final ``_atomic_write``, so a forced re-stitch
    that fails leaves the existing cached hero untouched; a successful one atomically
    replaces it and re-pins the mtime. A forced cold run always returns a non-empty
    ``StitchResult.projection`` (never the cache-hit ``''``).

    Raises :class:`HuginNotFound` when Hugin is absent (caller keeps the gallery) and
    :class:`StitchFailed` on any step failure, timeout, or degenerate output.
    """
    primary_source = Path(primary_source)
    tiles = [primary_source, *(Path(f) for f in frame_sources)]
    out = stitch_cache_path(cache_root, rel_key)
    newest = max(t.stat().st_mtime for t in tiles)
    if not force and out.exists() and out.stat().st_mtime >= newest:
        return StitchResult(out, "")  # cache hit: caller keeps the recorded projection

    tools = find_hugin(hugin_bin_dir)
    if tools is None:
        logger.warning(
            "panorama stitch unavailable for %s: Hugin CLI tools not found on PATH or "
            "under hugin_bin_dir=%s", primary_source.name, hugin_bin_dir,
        )
        raise HuginNotFound("Hugin CLI tools not found on PATH or under hugin_bin_dir")

    with tempfile.TemporaryDirectory(prefix="geosorter-stitch-") as tmp:
        work = Path(tmp)
        pto = str(work / "project.pto")
        prefix = str(work / "out")
        # The six pipeline steps in order — looped so each one is logged + reported
        # through on_step before it runs (the names match the _HUGIN_TOOLS order).
        # cpfind --celeste (cloud control-point removal) and autooptimiser -l (lens
        # geometry optimisation) are opt-out via config (m-frontend-pano-ux): both add
        # time and a user with clean skies / a fixed lens can drop them. The canvas is
        # also configurable and defaults to the smaller 4000x2000.
        cpfind_cmd = [tools["cpfind"], "--multirow"]
        if celeste:
            cpfind_cmd.append("--celeste")
        cpfind_cmd += ["-o", pto, pto]
        autoopt_cmd = [tools["autooptimiser"], "-a", "-m"]
        if optimise_lens:
            autoopt_cmd.append("-l")
        autoopt_cmd += ["-s", "-o", pto, pto]

        total = 6  # fixed 6-step pipeline (on_step contract is stable for the UI)

        def _do(index: int, name: str, cmd: list[str]) -> None:
            if on_step is not None:
                on_step(index, total, name)
            logger.info(
                "panorama stitch %s: step %d/%d %s", primary_source.name, index, total, name
            )
            started = time.perf_counter()
            _run_hugin(cmd)
            logger.info(
                "panorama stitch %s: step %d/%d %s done in %.1fs",
                primary_source.name, index, total, name, time.perf_counter() - started,
            )

        # Steps 1-4 project + align the tiles; autooptimiser -s estimates the output
        # geometry into the .pto. THEN read that geometry to pick the projection step 5
        # builds the canvas for (instead of hard-coding equirectangular).
        _do(1, "pto_gen", [tools["pto_gen"], "-o", pto, *[str(t) for t in tiles]])
        _do(2, "cpfind", cpfind_cmd)
        _do(3, "cpclean", [tools["cpclean"], "-o", pto, pto])
        _do(4, "autooptimiser", autoopt_cmd)

        hfov = _parse_pto_hfov(Path(pto).read_text())
        proj_code, kind = _choose_projection(hfov)
        if forced_projection is not None:
            # Manual override (map UI re-stitch): honour the user's explicit choice
            # instead of the HFOV-derived projection.
            proj_code, kind = _forced_projection_code(forced_projection)
            logger.info(
                "panorama stitch %s: forced projection %d (%s) [HFOV %.1f deg]",
                primary_source.name, proj_code, kind, hfov,
            )
        else:
            logger.info(
                "panorama stitch %s: HFOV %.1f deg -> projection %d (%s)",
                primary_source.name, hfov, proj_code, kind,
            )

        _do(5, "pano_modify", [tools["pano_modify"], f"--projection={proj_code}",
                               f"--canvas={canvas}", "--crop=AUTO", "-o", pto, pto])
        _do(6, "hugin_executor",
            [tools["hugin_executor"], "--stitching", f"--prefix={prefix}", pto])

        tifs = sorted(work.glob("out*.tif"))
        if not tifs:
            raise StitchFailed("hugin_executor produced no output TIFF")
        tif = tifs[0]
        # A single-row 360 sweep is chosen equirectangular by HFOV, but --crop=AUTO
        # yields a wide thin strip whose aspect is well outside the equirectangular
        # envelope. It is a legitimate FLAT panning image, not a 2:1 sphere — reclassify
        # so it passes the (wider) flat gate and routes to the flat hero instead of being
        # rejected. Reuses the already-rendered output (no second Hugin pass). The
        # reclassification is ONE-WAY (equirectangular -> flat only): a true full sphere
        # renders within [1.3, 3.0] so the guard below is false and it stays
        # equirectangular; a flat is never promoted to equirectangular.
        if kind == "equirectangular" and forced_projection is None:
            with Image.open(tif) as _im:
                _w, _h = _im.size
            _aspect = _w / _h if _h else 0.0
            if (not (STITCH_MIN_ASPECT <= _aspect <= STITCH_MAX_ASPECT)
                    and STITCH_FLAT_MIN_ASPECT <= _aspect <= STITCH_FLAT_MAX_ASPECT):
                logger.info(
                    "panorama stitch %s: equirectangular output aspect %.2f outside "
                    "[%.1f, %.1f] -> reclassifying as flat",
                    primary_source.name, _aspect, STITCH_MIN_ASPECT, STITCH_MAX_ASPECT,
                )
                kind = "flat"
        _stitch_gate(tif, kind)  # projection-aware; raises before anything is cached

        def _produce(dest: Path) -> None:
            with Image.open(tif) as img:
                img = img.convert("RGB")
                if max(img.size) > STITCH_LONG_EDGE_CAP:
                    img.thumbnail((STITCH_LONG_EDGE_CAP, STITCH_LONG_EDGE_CAP))
                img.save(dest, "JPEG", quality=90)

        _atomic_write(out, _produce)
        _touch_to_mtime(out, newest)  # pin freshness to the newest tile (SMB-safe)
    return StitchResult(out, kind)
