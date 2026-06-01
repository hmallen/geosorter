"""Lazy, cached derived-asset generation for the map viewer (B6).

Thumbnails, video poster frames, and HEVC->H.264 playback proxies are generated
**on first request** and cached under ``library_root/.geosorter-cache/`` so the
crash-safe Phase 0 ``organize`` pipeline never has to depend on Pillow/ffmpeg.

Each asset mirrors its source's library-relative path under a per-kind cache
subdirectory (``thumbs``/``posters``/``proxies``). Freshness is mtime-based: a
cached file is reused only while it is at least as new as its source, so a
re-organized file regenerates without any hashing.

ffmpeg/ffprobe are invoked as list-form subprocesses (mirroring
:mod:`geosorter.metadata`); only Pillow is a Python dependency.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from PIL import Image, ImageOps

THUMB_MAX = 512
CACHE_DIRNAME = ".geosorter-cache"


def _cache_path(library_root: Path | str, source: Path, kind: str, ext: str) -> Path:
    """Cache file for ``source`` under ``library_root/.geosorter-cache/<kind>/``.

    The source's library-relative path is mirrored under the kind directory; a
    source outside ``library_root`` falls back to its bare filename.
    """
    library_root = Path(library_root)
    source = Path(source)
    try:
        rel = source.resolve().relative_to(library_root.resolve())
    except ValueError:
        rel = Path(source.name)
    return (library_root / CACHE_DIRNAME / kind / rel).with_suffix(ext)


def _is_fresh(cache_file: Path, source: Path) -> bool:
    """True if the cached file exists and is no older than its source."""
    return cache_file.exists() and cache_file.stat().st_mtime >= source.stat().st_mtime


def _run_ffmpeg(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
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


def thumbnail(library_root: Path | str, source: Path | str) -> Path:
    """Return a cached 512px JPEG thumbnail of an image, generating it if stale."""
    source = Path(source)
    out = _cache_path(library_root, source, "thumbs", ".jpg")
    if _is_fresh(out, source):
        return out

    def _produce(dest: Path) -> None:
        with Image.open(source) as img:
            img = ImageOps.exif_transpose(img)  # honour camera orientation
            img.thumbnail((THUMB_MAX, THUMB_MAX))
            img.convert("RGB").save(dest, "JPEG", quality=85)

    _atomic_write(out, _produce)
    return out


def poster(library_root: Path | str, source: Path | str) -> Path:
    """Return a cached JPEG poster frame for a video, generating it if stale."""
    source = Path(source)
    out = _cache_path(library_root, source, "posters", ".jpg")
    if _is_fresh(out, source):
        return out
    _atomic_write(
        out,
        lambda dest: _run_ffmpeg(
            ["ffmpeg", "-v", "error", "-y", "-ss", "0", "-i", str(source),
             "-frames:v", "1", str(dest)]
        ),
    )
    return out


def proxy(library_root: Path | str, source: Path | str, codec: str | None) -> Path:
    """Return a browser-playable video path for ``source``.

    H.264 (or unknown) sources are already streamable and are returned unchanged.
    HEVC sources are transcoded to a cached H.264 proxy under ``proxies/``.
    """
    source = Path(source)
    if codec != "h265":
        return source
    out = _cache_path(library_root, source, "proxies", ".mp4")
    if _is_fresh(out, source):
        return out
    _atomic_write(
        out,
        lambda dest: _run_ffmpeg(
            ["ffmpeg", "-v", "error", "-y", "-i", str(source),
             "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart", str(dest)]
        ),
    )
    return out
