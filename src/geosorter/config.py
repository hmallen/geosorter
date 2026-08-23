"""Configuration loading for geosorter.

Resolution order for the ``geosorter.toml`` location:

1. an explicit ``--config PATH`` (passed to :func:`load` / :func:`write_starter`)
2. the ``GEOSORTER_CONFIG`` environment variable
3. ``./geosorter.toml`` in the current directory, when it exists (the
   repo-local-config convention — running any verb from a checkout that keeps
   its gitignored config at the root Just Works without ``--config``)
4. ``platformdirs.user_config_dir("geosorter")/geosorter.toml``

The two SQLite databases default to ``platformdirs.user_data_dir("geosorter")``
— always on local disk, never inside ``library_root``.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

import platformdirs

APP_NAME = "geosorter"

_STARTER_TEMPLATE = """\
# geosorter configuration

# Set these before running `geosorter organize`:
# inbox_path = 'D:\\drone\\inbox'
# library_root = 'Z:\\DroneLibrary'

# Local-disk databases (kept off library_root). Defaults shown.
index_db_path = '{index_db}'
geonames_db_path = '{geonames_db}'

# Spatial index for geocoding: 'rtree' (default) or 'columnar' (fallback).
spatial_index = 'rtree'

# Admin password hash. When set, the map UI is view-only until login and the
# mutating API routes (organize/undo/rescan/retag/assign/stitch) require a valid
# admin token; unset means the app and API are fully open. Do NOT write a plaintext
# password here — generate the hash with `geosorter set-admin-password`.
# admin_password_hash = 'pbkdf2_sha256$...'

# Prefer-nearest-feature radius (km). A named park/peak/hydro feature wins over
# the nearest town when it lies within this distance of the capture coordinate.
# GeoNames features are point centroids, not polygons, so this is an
# approximation: a capture near the edge of a large park may fall outside the
# radius and resolve to a town instead. Raise it to favour feature names, lower
# it to favour towns. Requires `bootstrap --features` to have loaded L/T/H data.
# feature_proximity_km = 5.0

# Neighbor-GPS inference window (minutes). A capture missing GPS but carrying a
# timestamp borrows the location of the nearest-in-time GPS-tagged capture in the
# same `organize` run, but only when that capture is within this many minutes.
# Beyond the window the no-GPS file is quarantined as usual (never relocated far).
# inference_max_gap_minutes = 30.0

# Retain DJI hyperlapse source frames (the 250-350 stills in HYPERLAPSE/001_<counter>/)
# alongside the rendered video, filed into a `<render>_frames/` subfolder. Set to
# false to file only the render and leave the frames in the inbox (saves disk).
# retain_hyperlapse_frames = true

# Move a detected duplicate (content already in the library) into
# `<inbox>/_duplicates/<original-relpath>` and log it to
# `<inbox>/_duplicates/duplicates.log`, instead of silently leaving it in the inbox.
# Set false to restore the old skip-in-place behavior.
# relocate_duplicates = true

# Optional directory holding the Hugin CLI tools (pto_gen, cpfind, hugin_executor,
# ...), used to stitch a 360 panorama hero from a DJI PANORAMA tile set. Leave unset
# to detect them on PATH. When neither finds Hugin, panorama stitching is silently
# unavailable and the map UI keeps the tile gallery (no hard dependency).
# hugin_bin_dir = 'C:\\Program Files\\Hugin\\bin'

# Optional untrunc executable (or a directory holding it), used by the map UI's
# Repair panel to rebuild truncated MP4s (interrupted DJI recordings missing their
# moov atom) from a healthy reference clip. Leave unset to detect it on PATH; when
# neither finds untrunc, the Repair panel can still scan and delete broken files
# but offers no repair (no hard dependency).
# untrunc_path = 'C:\\Tools\\untrunc\\untrunc.exe'

# --- Panorama-stitch tuning (smaller/faster 360 stitch) ---
# The equirectangular output canvas (WIDTHxHEIGHT). Smaller = fewer output pixels
# = a faster stitch; the default shrank to 4000x2000 from 6000x3000.
# stitch_canvas = '4000x2000'
# cpfind --celeste removes cloud control points (good for skies, but slower). Set
# false to skip it. autooptimiser -l optimises lens geometry; set the other false
# to skip it. Both default on (best quality); disable for a faster stitch.
# stitch_celeste = true
# stitch_optimise_lens = true

# --- Upload-resilience knobs (sized for a multi-hour, 5-20k-file bulk import) ---
# Free-space headroom (GB) required over the source bytes before/during a move;
# a mid-run recheck aborts cleanly before the share fills.
# disk_margin_gb = 5.0
# Per-file copy attempts on a transient OSError (e.g. an SMB blip), with an
# exponential backoff whose base (seconds) is copy_retry_backoff_s.
# copy_retry_attempts = 3
# copy_retry_backoff_s = 0.5
# ExifTool pass-1 chunking: groups per daemon before a fresh restart, and the
# per-file extract attempts before a file is quarantined as unreadable.
# extract_chunk_size = 500
# extract_max_failures = 3

# --- Derived-cache tiering (keep hot thumbnails off the SMB share) ---
# Local-SSD cache for thumbnails/posters/previews. Defaults to the platformdirs
# user cache dir; must be absolute and NOT inside library_root or inbox_path.
# cache_dir = 'C:\\Users\\you\\AppData\\Local\\geosorter\\Cache'
# Tier for HEVC proxies + panorama stitches (large, written-once). Defaults to
# library_root; point it at an SSD-backed share to relocate. Absolute when set.
# proxy_cache_dir = 'Z:\\DroneLibrary'
# Local-tier eviction cap (GB), honoured by the post-organize warm pass's sweep.
# cache_max_gb = 10.0

# Pre-generate HEVC->H.264 playback proxies during the post-organize warm pass, so
# the first play of an HEVC clip is instant instead of paying the transcode on demand.
# Off by default (proxies are large and slow to transcode).
# warm_proxies = false
# Cap (GB) on the proxy/stitch tier's HEVC proxies. Unset = uncapped (proxies never
# evicted). When set, the warm pass evicts least-recently-used proxies down to this
# cap (panorama stitch heroes are never evicted by it).
# proxy_cache_max_gb = 50.0

# Hardware encoder for the HEVC->H.264 proxy transcode: 'auto' (default — use NVENC
# when an h264_nvenc-capable ffmpeg is detected, else libx264, and fall back to
# libx264 if an NVENC encode fails), 'nvenc' (force NVENC; an encode failure surfaces),
# or 'none' (always libx264 — the CPU path). NVENC needs an NVIDIA GPU + an ffmpeg
# built with h264_nvenc/CUDA; on any other machine 'auto' transparently uses libx264.
# proxy_hwaccel = 'auto'
"""


@dataclass(frozen=True)
class Config:
    """Resolved geosorter configuration."""

    inbox_path: Path | None
    library_root: Path | None
    index_db_path: Path
    geonames_db_path: Path
    spatial_index: str = "rtree"
    feature_proximity_km: float = 5.0
    inference_max_gap_minutes: float = 30.0
    retain_hyperlapse_frames: bool = True
    # When True (default), `organize` MOVES a detected duplicate (content already in the
    # library) into `<inbox>/_duplicates/<original-relpath>` and logs it, instead of
    # silently leaving it in the inbox. False restores the old skip-in-place behavior.
    relocate_duplicates: bool = True
    hugin_bin_dir: Path | None = None
    # untrunc executable (or a directory holding it) for the broken-capture repair
    # flow (m-repair-broken-captures). None → PATH lookup; when neither finds it the
    # Repair panel degrades to scan/delete-only (no hard dependency, like Hugin).
    untrunc_path: Path | None = None
    # Panorama-stitch tuning (m-frontend-pano-ux): the equirectangular canvas
    # (smaller = faster) and two opt-out quality steps — cpfind --celeste and
    # autooptimiser -l. Defaults match derived.STITCH_CANVAS / both steps on.
    stitch_canvas: str = "4000x2000"
    stitch_celeste: bool = True
    stitch_optimise_lens: bool = True
    # Organize-resilience knobs (m-organize-resilience) — sized for a multi-hour,
    # 5–20k-file bulk upload over SMB.
    disk_margin_gb: float = 5.0  # free-space headroom over the source bytes
    copy_retry_attempts: int = 3  # per-file copy attempts on a transient OSError
    copy_retry_backoff_s: float = 0.5  # base of the exponential copy-retry backoff
    extract_chunk_size: int = 500  # groups per ExifTool daemon before a fresh restart
    extract_max_failures: int = 3  # per-file extract attempts before quarantine
    # Derived-cache tiering (m-cache-tiering-safety). thumbs/posters/previews live on
    # the local SSD `cache_dir`; proxies/stitch on `proxy_cache_dir` (defaults to
    # `library_root` at use). `cache_dir` is None only on a directly-constructed
    # Config; `load()` fills it with `default_cache_dir()`.
    cache_dir: Path | None = None  # local-SSD cache for thumbs/posters/previews
    proxy_cache_dir: Path | None = None  # None → library_root (proxies/stitch tier)
    cache_max_gb: float = 10.0  # local-tier eviction cap (consumed by m-derived-at-scale)
    # Proxy pre-warm + proxy-tier cap (m-implement-proxy-prewarm-cap). When
    # `warm_proxies` is on, the post-organize warm pass also pre-generates HEVC→H.264
    # proxies (off by default — they are large and slow to transcode). `proxy_cache_max_gb`
    # caps the proxy/stitch tier's `proxies` kind; None = uncapped (today's never-evict
    # behavior), enforced at the warm-pass boundary INDEPENDENT of `warm_proxies`.
    warm_proxies: bool = False
    proxy_cache_max_gb: float | None = None
    # Admin-auth (m-implement-view-only-admin-auth): the PBKDF2 hash of the single
    # shared admin password (see auth.hash_password), set via
    # `geosorter set-admin-password`. None = no password configured -> the app and
    # API stay fully open (today's loopback-dev behaviour); when set, the map UI is
    # view-only until login and the mutating API routes require a valid bearer token.
    admin_password_hash: str | None = None
    # Hardware-encoder selection for the HEVC->H.264 proxy transcode
    # (#124): 'auto' (NVENC when detected, else libx264, fall back to libx264 on a
    # failed NVENC encode), 'nvenc' (force NVENC — strict, a failure surfaces), or
    # 'none' (always libx264). Validated at load.
    proxy_hwaccel: str = "auto"


def default_data_dir() -> Path:
    # appauthor=False avoids the Windows "geosorter\geosorter" double nesting.
    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))


def default_cache_dir() -> Path:
    """Local-SSD default for the derived cache (off the SMB library share)."""
    return Path(platformdirs.user_cache_dir(APP_NAME, appauthor=False))


def resolve_proxy_cache_dir(cfg) -> Path:
    r"""The proxy/stitch cache tier for ``cfg``: the explicit ``proxy_cache_dir`` or,
    when unset, the **raw** ``library_root``.

    Never ``.resolve()``d: the panorama-stitch generator (``jobs._run_stitch``) and the
    ``/api/stitch`` serve route both call this, and on a mapped SMB drive ``.resolve()``
    rewrites ``Z:\`` to a UNC path — so a resolved default would make the two disagree
    on the cached-hero path. Centralizing the default here makes that invariant
    structural rather than a convention duplicated across two modules.
    """
    return Path(cfg.proxy_cache_dir) if cfg.proxy_cache_dir else Path(cfg.library_root)


def default_config_path() -> Path:
    return (
        Path(platformdirs.user_config_dir(APP_NAME, appauthor=False))
        / "geosorter.toml"
    )


def resolve_config_path(explicit: str | Path | None = None) -> Path:
    """Resolve the config-file path per the documented precedence.

    Unlike the explicit/env sources (honored even when the file is absent, so a
    setter can report a clean "no config file" on the path the user named), the
    current-directory step only applies when ``./geosorter.toml`` actually
    exists — otherwise a checkout without one falls through to the platformdirs
    default rather than every command claiming the repo root as its config home.
    """
    if explicit:
        return Path(explicit)
    env = os.environ.get("GEOSORTER_CONFIG")
    if env:
        return Path(env)
    cwd_cfg = Path.cwd() / "geosorter.toml"
    if cwd_cfg.is_file():
        return cwd_cfg
    return default_config_path()


def _opt_path(value: object) -> Path | None:
    if not value:
        return None
    return Path(str(value)).expanduser()


def load(path: str | Path | None = None) -> Config:
    """Load configuration, applying defaults for anything unset.

    A missing config file is not an error — defaults are returned (the two DB
    paths fall under :func:`default_data_dir`).
    """
    cfg_path = resolve_config_path(path)
    data: dict[str, object] = {}
    if cfg_path.exists():
        with open(cfg_path, "rb") as fh:
            data = tomllib.load(fh)

    data_dir = default_data_dir()
    index_db = data.get("index_db_path") or (data_dir / "index.db")
    geonames_db = data.get("geonames_db_path") or (data_dir / "geonames.db")
    spatial_index = str(data.get("spatial_index", "rtree"))
    if spatial_index not in ("rtree", "columnar"):
        raise ValueError(f"invalid spatial_index in config: {spatial_index!r}")

    disk_margin_gb = float(data.get("disk_margin_gb", 5.0))
    copy_retry_attempts = int(data.get("copy_retry_attempts", 3))
    copy_retry_backoff_s = float(data.get("copy_retry_backoff_s", 0.5))
    extract_chunk_size = int(data.get("extract_chunk_size", 500))
    extract_max_failures = int(data.get("extract_max_failures", 3))
    if disk_margin_gb < 0:
        raise ValueError(f"disk_margin_gb must be >= 0: {disk_margin_gb!r}")
    if copy_retry_attempts < 1:
        raise ValueError(f"copy_retry_attempts must be >= 1: {copy_retry_attempts!r}")
    if copy_retry_backoff_s < 0:
        raise ValueError(f"copy_retry_backoff_s must be >= 0: {copy_retry_backoff_s!r}")
    if extract_chunk_size < 1:
        raise ValueError(f"extract_chunk_size must be >= 1: {extract_chunk_size!r}")
    if extract_max_failures < 1:
        raise ValueError(f"extract_max_failures must be >= 1: {extract_max_failures!r}")

    stitch_canvas = str(data.get("stitch_canvas", "4000x2000"))
    if not re.fullmatch(r"\d+x\d+", stitch_canvas):
        raise ValueError(f"stitch_canvas must be 'WIDTHxHEIGHT' (e.g. '4000x2000'): {stitch_canvas!r}")
    stitch_celeste = bool(data.get("stitch_celeste", True))
    stitch_optimise_lens = bool(data.get("stitch_optimise_lens", True))

    inbox_path = _opt_path(data.get("inbox_path"))
    library_root = _opt_path(data.get("library_root"))

    # Admin-auth: a blank/absent key means "no password configured" (open app).
    admin_password_hash = str(data["admin_password_hash"]) if data.get(
        "admin_password_hash"
    ) else None

    cache_dir = _opt_path(data.get("cache_dir")) or default_cache_dir()
    proxy_cache_dir = _opt_path(data.get("proxy_cache_dir"))
    cache_max_gb = float(data.get("cache_max_gb", 10.0))
    if not cache_dir.is_absolute():
        raise ValueError(f"cache_dir must be an absolute path: {cache_dir!r}")
    # The local cache must not live under the SMB library or the inbox (the whole
    # point is to keep hot reads off the LAN; nesting it back in would defeat that).
    for guard, name in ((library_root, "library_root"), (inbox_path, "inbox_path")):
        if guard is not None and cache_dir.is_relative_to(guard):
            raise ValueError(f"cache_dir must not be inside {name}: {cache_dir!r}")
    if proxy_cache_dir is not None and not proxy_cache_dir.is_absolute():
        raise ValueError(f"proxy_cache_dir must be an absolute path: {proxy_cache_dir!r}")
    if cache_max_gb <= 0:
        raise ValueError(f"cache_max_gb must be > 0: {cache_max_gb!r}")

    warm_proxies = bool(data.get("warm_proxies", False))
    proxy_cache_max_gb = data.get("proxy_cache_max_gb")
    if proxy_cache_max_gb is not None:
        proxy_cache_max_gb = float(proxy_cache_max_gb)
        if proxy_cache_max_gb <= 0:
            raise ValueError(
                f"proxy_cache_max_gb must be > 0: {proxy_cache_max_gb!r}"
            )

    proxy_hwaccel = str(data.get("proxy_hwaccel", "auto"))
    if proxy_hwaccel not in ("auto", "nvenc", "none"):
        raise ValueError(
            f"proxy_hwaccel must be 'auto', 'nvenc', or 'none': {proxy_hwaccel!r}"
        )

    return Config(
        inbox_path=inbox_path,
        library_root=library_root,
        index_db_path=Path(str(index_db)).expanduser(),
        geonames_db_path=Path(str(geonames_db)).expanduser(),
        spatial_index=spatial_index,
        feature_proximity_km=float(data.get("feature_proximity_km", 5.0)),
        inference_max_gap_minutes=float(data.get("inference_max_gap_minutes", 30.0)),
        retain_hyperlapse_frames=bool(data.get("retain_hyperlapse_frames", True)),
        relocate_duplicates=bool(data.get("relocate_duplicates", True)),
        hugin_bin_dir=_opt_path(data.get("hugin_bin_dir")),
        untrunc_path=_opt_path(data.get("untrunc_path")),
        stitch_canvas=stitch_canvas,
        stitch_celeste=stitch_celeste,
        stitch_optimise_lens=stitch_optimise_lens,
        disk_margin_gb=disk_margin_gb,
        copy_retry_attempts=copy_retry_attempts,
        copy_retry_backoff_s=copy_retry_backoff_s,
        extract_chunk_size=extract_chunk_size,
        extract_max_failures=extract_max_failures,
        cache_dir=cache_dir,
        proxy_cache_dir=proxy_cache_dir,
        cache_max_gb=cache_max_gb,
        warm_proxies=warm_proxies,
        proxy_cache_max_gb=proxy_cache_max_gb,
        admin_password_hash=admin_password_hash,
        proxy_hwaccel=proxy_hwaccel,
    )


def _persist_key(path: str | Path | None, key: str, value: str) -> bool:
    """Rewrite one ``key = '<value>'`` line in an existing config file.

    Replaces the exact key token in place (never a comment or a key that merely
    starts with the same text), appending the line when the key is absent.
    Returns ``False`` (no-op) when the config file does not exist — the setter
    commands should not silently create one.
    """
    cfg_path = resolve_config_path(path)
    if not cfg_path.exists():
        return False
    lines = cfg_path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    found = False
    for line in lines:
        line_key = line.split("=", 1)[0].strip() if "=" in line else ""
        if line_key == key:
            out.append(f"{key} = '{value}'")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key} = '{value}'")
    cfg_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return True


def update_spatial_index(path: str | Path | None, value: str) -> bool:
    """Persist the effective ``spatial_index`` to an existing config file."""
    if value not in ("rtree", "columnar"):
        raise ValueError(f"invalid spatial_index: {value!r}")
    return _persist_key(path, "spatial_index", value)


def set_admin_password_hash(path: str | Path | None, value: str) -> bool:
    """Persist ``admin_password_hash`` into an existing config file.

    ``value`` is the :func:`geosorter.auth.hash_password` string.
    """
    return _persist_key(path, "admin_password_hash", value)


def set_untrunc_path(path: str | Path | None, value: str) -> bool:
    """Persist ``untrunc_path`` into an existing config file.

    Written by ``geosorter install-untrunc`` after a successful install, so the
    map UI's Repair panel finds the binary without a manual config edit.
    """
    return _persist_key(path, "untrunc_path", value)


def write_starter(path: str | Path | None = None, *, overwrite: bool = False) -> Path:
    """Write a starter ``geosorter.toml`` and return its path."""
    cfg_path = resolve_config_path(path)
    if cfg_path.exists() and not overwrite:
        raise FileExistsError(cfg_path)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    data_dir = default_data_dir()
    content = _STARTER_TEMPLATE.format(
        index_db=data_dir / "index.db",
        geonames_db=data_dir / "geonames.db",
    )
    cfg_path.write_text(content, encoding="utf-8")
    return cfg_path
