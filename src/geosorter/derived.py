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

import logging
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from PIL import Image, ImageOps

logger = logging.getLogger("geosorter.derived")

THUMB_MAX = 512
PREVIEW_MAX = 1920
CACHE_DIRNAME = ".geosorter-cache"
# Hard ceiling on a single proxy transcode (a full HEVC->H.264 of a multi-minute 4K
# clip over SMB is slow, but 30 min only fires on a genuinely stuck process).
FFMPEG_TIMEOUT_S = 1800

# --- Panorama stitch (B13) -------------------------------------------------
# The 360 equirectangular canvas. pano_modify sizes the full sphere to this; a
# 2:1 canvas keeps a longitude:latitude ratio, and --crop=AUTO trims the empty
# margins (so the final long edge is bounded by STITCH_LONG_EDGE_CAP).
STITCH_CANVAS = "6000x3000"
STITCH_LONG_EDGE_CAP = 6000
# Output-validity gate: a plausible equirectangular hero is wide, large, and
# (unlike the cv2 failure mode) not a mostly-black void.
STITCH_MIN_LONG_EDGE = 2000
STITCH_MIN_ASPECT = 1.3
STITCH_MAX_ASPECT = 3.0
STITCH_MAX_BLACK_FRAC = 0.15
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

    _atomic_write(out, _produce)
    _touch_to_source(out, source)
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
    _atomic_write(
        out,
        lambda dest: _run_ffmpeg(
            ["ffmpeg", "-v", "error", "-y", "-ss", "0", "-i", str(source),
             "-frames:v", "1", str(dest)]
        ),
    )
    _touch_to_source(out, source)
    return out


def proxy(cache_root: Path | str, rel_key: str, source: Path | str, codec: str | None) -> Path:
    """Return a browser-playable video path for ``source``.

    H.264 (or unknown) sources are already streamable and are returned unchanged.
    HEVC sources are transcoded to a cached H.264 proxy under ``proxies/``.
    """
    source = Path(source)
    if codec != "h265":
        return source
    out = _cache_path(cache_root, rel_key, "proxies", ".mp4")
    if _is_fresh(out, source):
        return out
    _atomic_write(
        out,
        lambda dest: _run_ffmpeg(
            ["ffmpeg", "-v", "error", "-y", "-i", str(source),
             "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart", str(dest)]
        ),
    )
    _touch_to_source(out, source)
    return out


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


def _stitch_gate(path: Path) -> None:
    """Raise :class:`StitchFailed` unless ``path`` is a plausible equirectangular.

    The cv2 failure mode the spike rejected was a warped partial with a ~45% black
    void. The gate verifies a sane size, a wide (equirectangular-like) aspect, and a
    near-black-pixel fraction under :data:`STITCH_MAX_BLACK_FRAC`, so a degenerate or
    failed stitch is never cached or served.
    """
    with Image.open(path) as img:
        width, height = img.size
        long_edge = max(width, height)
        if not (STITCH_MIN_LONG_EDGE <= long_edge <= STITCH_LONG_EDGE_CAP):
            raise StitchFailed(f"stitch long-edge {long_edge}px out of range")
        aspect = width / height if height else 0.0
        if not (STITCH_MIN_ASPECT <= aspect <= STITCH_MAX_ASPECT):
            raise StitchFailed(f"stitch aspect {aspect:.2f} not equirectangular-like")
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
    on_step: Callable[[int, int, str], None] | None = None,
) -> Path:
    """Return a cached 360 equirectangular JPEG stitched from a panorama's tiles.

    Runs the spike-proven Hugin pipeline (``pto_gen -> cpfind --multirow --celeste
    -> cpclean -> autooptimiser -> pano_modify (equirectangular) -> hugin_executor``)
    in a private temp dir, each step a list-form subprocess with a timeout, strictly
    off the crash-safe move path (called only by the background stitch job). The
    result is mtime-cached under ``library_root/.geosorter-cache/stitch/`` (fresh
    while no tile is newer than the cache) and validity-gated before caching.

    Each pipeline step is logged (with its elapsed time) and, when ``on_step`` is
    given, reported as ``on_step(step_index, step_total, step_name)`` *before* the
    step runs — so the background job and the map UI can show which of the six steps
    is currently executing during the multi-minute run.

    Raises :class:`HuginNotFound` when Hugin is absent (caller keeps the gallery) and
    :class:`StitchFailed` on any step failure, timeout, or degenerate output.
    """
    primary_source = Path(primary_source)
    tiles = [primary_source, *(Path(f) for f in frame_sources)]
    out = stitch_cache_path(cache_root, rel_key)
    newest = max(t.stat().st_mtime for t in tiles)
    if out.exists() and out.stat().st_mtime >= newest:
        return out

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
        steps: list[tuple[str, list[str]]] = [
            ("pto_gen", [tools["pto_gen"], "-o", pto, *[str(t) for t in tiles]]),
            ("cpfind", [tools["cpfind"], "--multirow", "--celeste", "-o", pto, pto]),
            ("cpclean", [tools["cpclean"], "-o", pto, pto]),
            ("autooptimiser", [tools["autooptimiser"], "-a", "-m", "-l", "-s", "-o", pto, pto]),
            ("pano_modify", [tools["pano_modify"], "--projection=2", f"--canvas={STITCH_CANVAS}",
                             "--crop=AUTO", "-o", pto, pto]),
            ("hugin_executor", [tools["hugin_executor"], "--stitching", f"--prefix={prefix}", pto]),
        ]
        total = len(steps)
        for index, (name, cmd) in enumerate(steps, start=1):
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

        tifs = sorted(work.glob("out*.tif"))
        if not tifs:
            raise StitchFailed("hugin_executor produced no output TIFF")
        tif = tifs[0]
        _stitch_gate(tif)  # raises before anything is cached

        def _produce(dest: Path) -> None:
            with Image.open(tif) as img:
                img = img.convert("RGB")
                if max(img.size) > STITCH_LONG_EDGE_CAP:
                    img.thumbnail((STITCH_LONG_EDGE_CAP, STITCH_LONG_EDGE_CAP))
                img.save(dest, "JPEG", quality=90)

        _atomic_write(out, _produce)
        _touch_to_mtime(out, newest)  # pin freshness to the newest tile (SMB-safe)
    return out
