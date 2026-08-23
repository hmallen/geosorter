r"""Repair truncated video captures with untrunc (m-repair-broken-captures).

A DJI recording interrupted mid-write (battery pull, crash, card ejection) never
gets its MP4 ``moov`` index atom, so the clip is undecodable: ffprobe reports
``moov atom not found``, no metadata (and so no GPS) can be extracted — which is
why these clips end up quarantined in ``_no-gps/`` — and the map UI shows the
"media unavailable" placeholder. The video DATA is usually still in the file;
`untrunc <https://github.com/anthwlock/untrunc>`_ rebuilds the missing index by
example, using a HEALTHY reference clip recorded by the same drone at the same
settings.

This module is the backend of the map UI's Repair panel:

* :func:`scan_broken` — probe every quarantined capture with ffprobe and report
  the broken ones (``zero-byte`` / ``no-moov`` / ``decode-error`` / ``missing``).
* :func:`reference_candidates` — rank healthy library videos as untrunc
  references for one broken file: same DJI naming series first, then a sibling
  segment of the same split recording, closest sequence number, closest capture
  date. The top-scoring candidate is flagged ``recommended``.
* :func:`run_repair` — copy the broken file into ``_repair/backups/`` (untrunc
  only ever READS that copy; the library original is untouched until an explicit
  accept), run untrunc against the chosen reference, and ffprobe-verify the
  output, which lands in ``_repair/fixed/`` for the user to preview.
* :func:`accept_repair` — swap the verified output onto the capture's
  ``dest_path``, refresh the row's sha256/codec/width/height/duration, and
  invalidate the derived cache so the placeholder poster regenerates. The
  pre-repair original stays in ``_repair/backups/`` as the safety copy.
* :func:`discard_repair` — drop an unaccepted output (and its backup — the
  library original was never modified).
* :func:`delete_broken` — delete a broken capture from disk (a 0-byte file has
  nothing to recover) and prune its index rows. Re-probes on the server and
  REFUSES a file that probes healthy, so the endpoint can never delete good
  media.

Everything untrunc touches lives under ``<library_root>/_repair/`` — a tree
organize never scans (it reads the inbox only) and rescan never prunes (it walks
index rows only), yet servable through the existing ``/api`` media routes for
the pre-accept preview. Repaired clips still carry NO capture metadata (the
rebuilt ``moov`` is cloned from the reference), so an accepted capture stays
quarantined for the normal assign-location flow.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import zipfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import config, db, pathing, rescan
from .derived import invalidate as invalidate_cache
from .move_engine import sha256_file

REPAIR_DIRNAME = "_repair"
_BACKUPS = "backups"
_FIXED = "fixed"

PROBE_TIMEOUT_S = 120
# Scan-time ffprobe concurrency: SMB round-trip latency dominates a probe, so a
# few parallel workers cut a ~200-file sweep from minutes to tens of seconds.
PROBE_WORKERS = 4
# A multi-GB rebuild over SMB is slow but bounded; this is a hang backstop, not a
# pacing expectation.
UNTRUNC_TIMEOUT_S = 4 * 3600
_COPY_CHUNK = 1 << 20


# Shared long-path prefix handling (single UNC-aware implementation).
_strip = pathing.strip_long_prefix


class UntruncNotFound(RuntimeError):
    """No untrunc executable was found (config ``untrunc_path`` or PATH)."""


def find_untrunc(untrunc_path: Path | str | None = None) -> str | None:
    """Locate the untrunc executable, or ``None`` when unavailable.

    ``untrunc_path`` (config) may be the executable itself or a directory holding
    it (mirroring ``hugin_bin_dir``); unset falls back to PATH lookup. Absence is
    not an error — the Repair panel degrades to scan/delete-only.
    """
    if untrunc_path:
        p = Path(untrunc_path)
        if p.is_dir():
            for name in ("untrunc.exe", "untrunc"):
                candidate = p / name
                if candidate.is_file():
                    return str(candidate)
            return None
        return str(p) if p.is_file() else None
    return shutil.which("untrunc")


# --------------------------------------------------------------------------- #
# Installer (`geosorter install-untrunc`)
# --------------------------------------------------------------------------- #

# The maintained untrunc fork's release feed. Pinned to the official repo — the
# installer must never be steered to another host by config or user input.
UNTRUNC_RELEASE_API = (
    "https://api.github.com/repos/anthwlock/untrunc/releases/latest"
)
DOWNLOAD_TIMEOUT_S = 300


@dataclass(frozen=True)
class InstallResult:
    """Outcome of one :func:`install_untrunc` run."""

    exe_path: Path
    asset_name: str
    size: int
    release_tag: str


def default_untrunc_dir() -> Path:
    """Where the installer unpacks untrunc: ``<data-dir>/tools/untrunc``."""
    return config.default_data_dir() / "tools" / "untrunc"


def select_untrunc_asset(assets: list[dict]) -> dict:
    """Pick the Windows build from a release's asset list (x64 first, x32 fallback)."""

    def find(token: str) -> dict | None:
        for asset in assets:
            name = str(asset.get("name", "")).lower()
            if token in name and name.endswith(".zip"):
                return asset
        return None

    chosen = find("x64") or find("x32")
    if chosen is None:
        raise RuntimeError(
            "the latest untrunc release carries no Windows .zip asset — "
            "install it manually from https://github.com/anthwlock/untrunc"
        )
    return chosen


def _fetch_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "geosorter", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — pinned https URL
        return json.load(resp)


def _fetch_to_file(url: str, dest: Path, on_bytes) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "geosorter"})
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_S) as resp, \
            open(dest, "wb") as out:  # noqa: S310 — asset URL from the pinned API
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            block = resp.read(1 << 16)
            if not block:
                break
            out.write(block)
            done += len(block)
            if on_bytes is not None:
                on_bytes(done, total)


def _smoke_test(exe: Path) -> None:
    """Run the freshly installed binary and demand its usage banner.

    Catches a broken download / missing DLL immediately, instead of at the first
    real repair attempt.
    """
    proc = subprocess.run(
        [str(exe)], capture_output=True, text=True, errors="replace", timeout=60,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    if "usage" not in out.lower():
        raise RuntimeError(
            f"the installed untrunc did not print its usage banner "
            f"(exit {proc.returncode}) — the download may be broken"
        )


def install_untrunc(dest_dir: Path | str | None = None, *, on_bytes=None,
                    fetch_json=None, fetch_to_file=None, verify=None) -> InstallResult:
    """Download the official untrunc Windows build and unpack it into ``dest_dir``.

    Fetches the latest release from the pinned anthwlock/untrunc repo, picks the
    x64 zip (x32 fallback), streams it down (``on_bytes(done, total)`` progress),
    flattens the archive into ``dest_dir`` (default
    :func:`default_untrunc_dir`) — the exe ships beside its ffmpeg DLLs, which
    must stay in one folder — and smoke-tests the binary. Windows-only: other
    platforms build untrunc from source, and a RuntimeError says so. The three
    network/exec seams are injectable for tests.
    """
    if os.name != "nt":
        raise RuntimeError(
            "install-untrunc only automates the Windows build; on this platform "
            "build untrunc from source: https://github.com/anthwlock/untrunc"
        )
    fetch_json = fetch_json or _fetch_json
    fetch_to_file = fetch_to_file or _fetch_to_file
    verify = verify or _smoke_test

    release = fetch_json(UNTRUNC_RELEASE_API)
    asset = select_untrunc_asset(release.get("assets", []))

    dest = Path(dest_dir) if dest_dir else default_untrunc_dir()
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest / str(asset["name"])
    fetch_to_file(str(asset["browser_download_url"]), archive, on_bytes)

    exe: Path | None = None
    try:
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                # Flatten the zip's untrunc_x64/ folder; taking only the basename
                # also makes any zip-slip path in the archive inert.
                target = dest / Path(info.filename).name
                with z.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                if target.name.lower() == "untrunc.exe":
                    exe = target
    finally:
        archive.unlink(missing_ok=True)

    if exe is None:
        raise RuntimeError(f"{asset['name']} did not contain untrunc.exe")
    verify(exe)
    return InstallResult(
        exe_path=exe, asset_name=str(asset["name"]),
        size=int(asset.get("size", 0)), release_tag=str(release.get("tag_name", "")),
    )


# --------------------------------------------------------------------------- #
# ffprobe classification
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of one ffprobe pass over a media file."""

    ok: bool
    error: str | None = None
    codec: str | None = None  # normalized 'h264'/'h265' when recognized
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None


def _normalize_codec(name: str | None) -> str | None:
    """Map an ffprobe codec_name to the index DB's ``'h264'``/``'h265'`` values."""
    if not name:
        return None
    low = name.lower()
    if low in ("h264", "avc", "avc1"):
        return "h264"
    if low in ("hevc", "h265", "hvc1", "hev1"):
        return "h265"
    return None


def probe_file(path: str | Path) -> ProbeResult:
    """ffprobe ``path``: decodable? Plus codec/resolution/duration when it is.

    Never raises on a bad file — a failure comes back as ``ok=False`` with the
    last stderr line (the classifier greps it for ``moov atom not found``). A
    missing ffprobe binary surfaces as an ``ok=False`` result too, so a scan
    degrades to reporting rather than crashing the job.
    """
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "stream=codec_type,codec_name,width,height:format=duration",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ProbeResult(ok=False, error=str(exc))
    err = (proc.stderr or "").strip()
    if proc.returncode != 0 or err:
        # Keep the WHOLE stderr (capped): the decisive "moov atom not found" is
        # an earlier line than ffprobe's final "Invalid data found" summary, and
        # _classify greps for it.
        detail = err if err else f"ffprobe exit {proc.returncode}"
        return ProbeResult(ok=False, error=detail[:500])
    codec = width = height = duration = None
    try:
        payload = json.loads(proc.stdout or "{}")
        for stream in payload.get("streams", []):
            if stream.get("codec_type") == "video":
                codec = _normalize_codec(stream.get("codec_name"))
                width = stream.get("width")
                height = stream.get("height")
                break
        raw_duration = payload.get("format", {}).get("duration")
        duration = float(raw_duration) if raw_duration is not None else None
    except (ValueError, TypeError):
        pass  # a parse hiccup degrades to "decodable, details unknown"
    return ProbeResult(ok=True, codec=codec, width=width, height=height,
                       duration_s=duration)


def _classify(probe: ProbeResult) -> tuple[str, str | None]:
    """Map a failed probe to the wire status: ``no-moov`` vs generic ``decode-error``."""
    error = probe.error or "undecodable"
    if "moov atom not found" in error.lower():
        return "no-moov", error
    return "decode-error", error


# --------------------------------------------------------------------------- #
# Scan
# --------------------------------------------------------------------------- #


@dataclass
class BrokenItem:
    """One quarantined capture that failed the disk/ffprobe check."""

    file_id: int
    filename: str
    media_type: str
    date: str | None
    dest_path: str  # stored (possibly \\?\-prefixed) form
    rel_path: str  # library-relative POSIX, for media URLs
    size: int
    status: str  # 'zero-byte' | 'no-moov' | 'decode-error' | 'missing'
    error: str | None = None


@dataclass
class ScanReport:
    """Outcome of one :func:`scan_broken` pass."""

    checked: int = 0
    ok: int = 0
    untrunc_available: bool = False
    items: list[BrokenItem] = field(default_factory=list)


_DATE_FOLDER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _capture_date(local_date: str | None, dest_path: str) -> str | None:
    """The capture's date: the stored ``local_date``, else the quarantine folder name.

    Broken clips carry no readable metadata, so their ``local_date`` is NULL — but
    organize files them under ``_no-gps/<file-mtime date>/``, which is the best
    available session hint for reference ranking and display.
    """
    if local_date:
        return local_date
    parent = Path(_strip(dest_path)).parent.name
    return parent if _DATE_FOLDER_RE.fullmatch(parent) else None


def scan_broken(cfg, *, progress=None, probe_fn=None) -> ScanReport:
    """Probe every quarantined capture and report the broken ones.

    Photos are size-checked only (untrunc targets MP4s; a corrupt-but-nonempty
    photo already renders as a placeholder and has no repair path here); videos
    get a full ffprobe. ``progress`` is a one-arg per-file callback (mirrors
    :func:`geosorter.rescan.run_rescan`). Read-only — no DB write, no file write.
    """
    # Late-bound default (not a def-time one) so tests can monkeypatch probe_file.
    probe_fn = probe_fn if probe_fn is not None else probe_file
    index = db.connect(cfg.index_db_path, integrity_check=False)
    report = ScanReport(untrunc_available=find_untrunc(cfg.untrunc_path) is not None)
    try:
        rows = index.execute(
            "SELECT id, filename, media_type, local_date, dest_path "
            "FROM files WHERE status='quarantined' ORDER BY id"
        ).fetchall()
    finally:
        index.close()

    # Pass 1 (serial, cheap stats): settle everything decidable without ffprobe
    # and collect the videos that need one.
    to_probe: list[tuple[BrokenItem, str]] = []
    for file_id, filename, media_type, local_date, dest_path in rows:
        report.checked += 1
        path = _strip(dest_path)
        item = BrokenItem(
            file_id=file_id, filename=filename, media_type=media_type,
            date=_capture_date(local_date, dest_path), dest_path=dest_path,
            rel_path=pathing.library_rel_key(cfg.library_root, dest_path),
            size=0, status="missing",
        )
        if not os.path.exists(path):
            report.items.append(item)  # stale row — rescan's job; listed for visibility
        else:
            item.size = os.path.getsize(path)
            if item.size == 0:
                item.status = "zero-byte"
                report.items.append(item)
            elif media_type != "video":
                report.ok += 1
            else:
                to_probe.append((item, path))
                continue
        if progress is not None:
            progress(filename)

    # Pass 2: ffprobe the survivors on a small pool (SMB latency dominates each
    # probe). map() preserves submission order, so items stay id-ordered and the
    # progress ticks stay one-per-file.
    with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
        for (item, _), probe in zip(
            to_probe, pool.map(probe_fn, (path for _, path in to_probe))
        ):
            if progress is not None:
                progress(item.filename)
            if probe.ok:
                report.ok += 1
                continue
            item.status, item.error = _classify(probe)
            report.items.append(item)
    # Pass 2 appends probed failures after the pass-1 items — restore id order so
    # the panel lists captures stably regardless of which pass classified them.
    report.items.sort(key=lambda item: item.file_id)
    return report


# --------------------------------------------------------------------------- #
# Reference ranking
# --------------------------------------------------------------------------- #

# DJI's two video naming series. Same-series is the strongest cheap proxy for
# "same drone generation / same container layout", which is what untrunc needs.
#   classic:     DJI_0119.MP4, and split-file segments DJI_0119_2.MP4
#   timestamped: DJI_20240804182951_0017_D.MP4
_CLASSIC_RE = re.compile(r"^DJI_(\d{3,5})(?:_(\d+))?\.[A-Za-z0-9]+$")
_TIMESTAMPED_RE = re.compile(r"^DJI_(\d{14})_(\d{3,5})(?:_([A-Za-z0-9]+))?\.[A-Za-z0-9]+$")


@dataclass(frozen=True)
class _ParsedName:
    series: str  # 'classic' | 'timestamped' | 'other'
    seq: int | None = None
    segment: int | None = None
    ts: str | None = None  # YYYYMMDDHHMMSS (timestamped series only)


def parse_dji_name(filename: str) -> _ParsedName:
    """Split a DJI video filename into its naming series + sequence fields."""
    m = _CLASSIC_RE.fullmatch(filename)
    if m:
        return _ParsedName(
            series="classic", seq=int(m.group(1)),
            segment=int(m.group(2)) if m.group(2) else None,
        )
    m = _TIMESTAMPED_RE.fullmatch(filename)
    if m:
        return _ParsedName(series="timestamped", ts=m.group(1), seq=int(m.group(2)))
    return _ParsedName(series="other")


@dataclass
class RefCandidate:
    """One healthy library video ranked as an untrunc reference."""

    file_id: int
    filename: str
    rel_path: str
    date: str | None
    place_string: str | None
    codec: str | None
    width: int | None
    height: int | None
    duration_s: float | None
    score: int
    reasons: list[str] = field(default_factory=list)
    recommended: bool = False


def _date_delta_days(a: str | None, b: str | None) -> int | None:
    """Whole days between two YYYY-MM-DD strings, or None when either is unknown."""
    if not a or not b:
        return None
    try:
        from datetime import date

        ya, ma, da = (int(x) for x in a.split("-"))
        yb, mb, db_ = (int(x) for x in b.split("-"))
        return abs((date(ya, ma, da) - date(yb, mb, db_)).days)
    except ValueError:
        return None


_FAR = 10**6  # tie-break sentinel: "distance unknown" sorts after any real distance


def _score_candidate(
    target: _ParsedName, target_date: str | None, target_dir: str,
    name: str, cand_date: str | None, cand_dir: str,
) -> tuple[int, list[str], tuple[int, int]]:
    """Score one healthy video as a reference for the broken target (higher = better).

    Also returns a ``(seq distance, date distance)`` tie-break so an equal-score
    field (common when nothing is close: every same-series clip lands on the base
    100) still orders nearest-recording-first instead of oldest-row-first.
    """
    parsed = parse_dji_name(name)
    score = 0
    reasons: list[str] = []
    dseq = ddate = _FAR
    if parsed.series != "other" and parsed.series == target.series:
        score += 100
        reasons.append("same DJI naming series (same drone generation)")

        if target.seq is not None and parsed.seq is not None:
            dseq = abs(parsed.seq - target.seq)
        if (target.series == "classic" and target.seq is not None
                and parsed.seq == target.seq):
            # DJI splits long recordings at ~3.77 GB; a sibling segment of the SAME
            # recording is byte-for-byte the same camera settings — the ideal reference.
            score += 400
            reasons.append("segment of the same split recording")
        elif target.series == "timestamped" and target.ts and parsed.ts == target.ts:
            score += 400
            reasons.append("segment of the same recording (same timestamp)")
        elif dseq != _FAR:
            score += max(0, 60 - dseq)
            if dseq <= 3:
                reasons.append(f"adjacent in the recording sequence (±{dseq})")

    delta = _date_delta_days(target_date, cand_date)
    if delta is not None:
        ddate = delta
        score += max(0, 50 - min(delta, 50))
        if delta == 0:
            reasons.append("captured the same day")
    if cand_dir == target_dir:
        score += 25
        reasons.append("same library folder")
    return score, reasons, (dseq, ddate)


def reference_candidates(cfg, file_id: int, *, limit: int = 5) -> list[RefCandidate]:
    """Rank healthy library videos as untrunc references for ``file_id``.

    Candidates are every OTHER video row whose ``codec`` is known — organize only
    records a codec when the clip's metadata was extractable, so ``codec IS NOT
    NULL`` cheaply excludes the broken quarantined clips without ffprobing the
    whole library. The chosen reference is still ffprobe-verified at repair time.
    Raises :class:`ValueError` for an unknown ``file_id``.
    """
    index = db.connect(cfg.index_db_path, integrity_check=False)
    try:
        target_row = index.execute(
            "SELECT filename, local_date, dest_path FROM files WHERE id=?",
            (file_id,),
        ).fetchone()
        if target_row is None:
            raise ValueError(f"unknown file id {file_id}")
        rows = index.execute(
            "SELECT id, filename, local_date, dest_path, place_string, codec, "
            "width, height, duration_s FROM files "
            "WHERE media_type='video' AND codec IS NOT NULL AND id != ? "
            "ORDER BY id",
            (file_id,),
        ).fetchall()
    finally:
        index.close()

    target = parse_dji_name(target_row[0])
    target_date = _capture_date(target_row[1], target_row[2])
    target_dir = str(Path(_strip(target_row[2])).parent)

    candidates: list[tuple[tuple[int, int], RefCandidate]] = []
    for (cid, name, local_date, dest_path, place, codec, width, height,
         duration_s) in rows:
        cand_date = _capture_date(local_date, dest_path)
        score, reasons, tiebreak = _score_candidate(
            target, target_date, target_dir,
            name, cand_date, str(Path(_strip(dest_path)).parent),
        )
        candidates.append((tiebreak, RefCandidate(
            file_id=cid, filename=name,
            rel_path=pathing.library_rel_key(cfg.library_root, dest_path),
            date=cand_date, place_string=place, codec=codec,
            width=width, height=height, duration_s=duration_s, score=score,
            reasons=reasons,
        )))

    candidates.sort(key=lambda entry: (-entry[1].score, *entry[0], entry[1].file_id))
    top = [entry[1] for entry in candidates[:limit]]
    # "Recommended" only when there IS a best match: a strict winner with real
    # signal, not an arbitrary first row of an all-zero tie.
    if top and top[0].score > 0 and (len(top) == 1 or top[0].score > top[1].score):
        top[0].recommended = True
    return top


# --------------------------------------------------------------------------- #
# Repair (untrunc)
# --------------------------------------------------------------------------- #


@dataclass
class RepairResult:
    """Outcome of one :func:`run_repair` attempt."""

    file_id: int
    status: str  # 'ok' | 'failed'
    error: str | None = None
    # Set on an 'ok' result that still looks wrong — e.g. untrunc "succeeded" but
    # recovered almost none of the data (a mismatched reference typically yields a
    # tiny decodable stub). Surfaced prominently at the verification step.
    warning: str | None = None
    fixed_path: str | None = None  # absolute path under _repair/fixed/
    fixed_rel: str | None = None  # library-relative POSIX, for preview URLs
    backup_path: str | None = None
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None
    size: int | None = None
    output_tail: list[str] = field(default_factory=list)


def _repair_dirs(cfg) -> tuple[Path, Path]:
    """The ``(_repair/backups, _repair/fixed)`` work folders (created on demand).

    Both live under the RAW ``library_root`` (never ``.resolve()``d — the mapped-
    drive invariant every path in this codebase follows), so the fixed output is
    inside the library tree and servable by the existing media routes.
    """
    root = Path(cfg.library_root) / REPAIR_DIRNAME
    backups, fixed = root / _BACKUPS, root / _FIXED
    backups.mkdir(parents=True, exist_ok=True)
    fixed.mkdir(parents=True, exist_ok=True)
    return backups, fixed


def _work_name(file_id: int, filename: str) -> str:
    # The id prefix keeps two same-named captures (a real case: the library holds
    # two distinct DJI_0076.MP4) from colliding in the shared work folders.
    return f"{file_id}_{filename}"


def _copy_with_progress(src: Path, dst: Path, on_bytes) -> None:
    """Chunked copy via a temp sibling + atomic replace, reporting cumulative bytes."""
    tmp = dst.with_name(dst.name + ".partial")
    copied = 0
    with open(src, "rb") as fin, open(tmp, "wb") as fout:
        for block in iter(lambda: fin.read(_COPY_CHUNK), b""):
            fout.write(block)
            copied += len(block)
            if on_bytes is not None:
                on_bytes(copied)
    os.replace(tmp, dst)


def _find_untrunc_output(backups: Path, work_name: str) -> Path | None:
    """Locate untrunc's output next to its input, tolerating both naming variants.

    ponchio/untrunc appends to the full input name (``clip.MP4_fixed.mp4``); the
    anthwlock fork replaces the extension (``clip_fixed.MP4``). Newest, largest
    match wins.
    """
    stem = Path(work_name).stem
    matches = {
        p
        for pattern in (f"{work_name}_fixed*", f"{stem}_fixed*")
        for p in backups.glob(pattern)
        if p.is_file()
    }
    if not matches:
        return None
    return max(matches, key=lambda p: (p.stat().st_size, p.stat().st_mtime))


def _run_untrunc(exe: str, reference: Path, broken_copy: Path,
                 on_poll) -> tuple[int, list[str]]:
    """Run untrunc, polling ``on_poll()`` about once a second while it works.

    Returns ``(returncode, output-tail)``; the tail is the last ~40 combined
    stdout/stderr lines for the failure report. A run exceeding
    :data:`UNTRUNC_TIMEOUT_S` is killed (returncode-independent callers treat a
    missing/unreadable output as the real failure signal). ``-n`` is the
    anthwlock fork's no-interactive flag — the job has no stdin, so a prompt
    would otherwise hang until the timeout (`install-untrunc` installs that
    fork; the legacy ponchio build, which lacks ``-n``, is not supported here).
    """
    proc = subprocess.Popen(
        [exe, "-n", str(reference), str(broken_copy)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace", cwd=str(broken_copy.parent),
    )
    tail: deque[str] = deque(maxlen=40)

    def _drain() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            stripped = line.rstrip()
            if stripped:
                tail.append(stripped)

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()
    deadline = time.monotonic() + UNTRUNC_TIMEOUT_S
    while True:
        try:
            rc = proc.wait(timeout=1.0)
            break
        except subprocess.TimeoutExpired:
            if on_poll is not None:
                on_poll()
            if time.monotonic() > deadline:
                proc.kill()
                proc.wait()
                tail.append(f"untrunc killed after {UNTRUNC_TIMEOUT_S}s timeout")
                rc = -1
                break
    reader.join(timeout=5)
    return rc, list(tail)


def run_repair(cfg, file_id: int, reference_id: int, *, progress=None,
               probe_fn=None, runner=None) -> RepairResult:
    """Attempt an untrunc repair of ``file_id`` using ``reference_id``.

    ``progress(phase, done_bytes, total_bytes)`` phases: ``'backup'`` (chunked
    copy into ``_repair/backups/``), ``'repair'`` (untrunc; ``done`` tracks the
    growing output), ``'verify'`` (ffprobe, indeterminate). The library original
    is NEVER an untrunc input or output — only the backup copy is. A failed
    attempt keeps the backup (re-running skips the re-copy) and reports the
    untrunc output tail. Raises :class:`UntruncNotFound` / :class:`ValueError`
    for setup errors; media-level failures come back as a ``'failed'`` result.
    """
    probe_fn = probe_fn if probe_fn is not None else probe_file
    exe = find_untrunc(cfg.untrunc_path)
    if runner is None:
        if exe is None:
            raise UntruncNotFound(
                "untrunc executable not found — install it and/or set untrunc_path "
                "in geosorter.toml"
            )
        runner = _run_untrunc

    def _emit(phase: str, done: int, total: int) -> None:
        if progress is not None:
            progress(phase, done, total)

    index = db.connect(cfg.index_db_path, integrity_check=False)
    try:
        target = index.execute(
            "SELECT filename, dest_path FROM files WHERE id=?", (file_id,)
        ).fetchone()
        ref = index.execute(
            "SELECT filename, dest_path FROM files WHERE id=?", (reference_id,)
        ).fetchone()
    finally:
        index.close()
    if target is None:
        raise ValueError(f"unknown file id {file_id}")
    if ref is None:
        raise ValueError(f"unknown reference file id {reference_id}")

    filename, dest_path = target
    source = Path(_strip(dest_path))
    result = RepairResult(file_id=file_id, status="failed")
    if not source.is_file():
        result.error = "the broken file is no longer on disk (run Rescan)"
        return result
    total = source.stat().st_size
    if total == 0:
        result.error = "the file is 0 bytes — there is no video data to recover"
        return result

    ref_path = Path(_strip(ref[1]))
    if not ref_path.is_file():
        result.error = f"reference file is missing on disk: {ref[0]}"
        return result
    ref_probe = probe_fn(ref_path)
    if not ref_probe.ok:
        result.error = f"reference file is not decodable itself: {ref_probe.error}"
        return result

    backups, fixed_dir = _repair_dirs(cfg)
    work_name = _work_name(file_id, filename)
    backup = backups / work_name
    result.backup_path = str(backup)

    # The safety copy untrunc will read. An earlier attempt's identical copy is
    # reused so a retry with a different reference skips the multi-GB re-copy.
    if not (backup.is_file() and backup.stat().st_size == total):
        _emit("backup", 0, total)
        _copy_with_progress(source, backup, lambda done: _emit("backup", done, total))

    # Drop any previous attempt's output so the post-run glob can't pick up a
    # stale file from a different reference.
    stale = _find_untrunc_output(backups, work_name)
    if stale is not None:
        stale.unlink(missing_ok=True)

    _emit("repair", 0, total)

    def _poll() -> None:
        out = _find_untrunc_output(backups, work_name)
        done = out.stat().st_size if out is not None else 0
        _emit("repair", min(done, total), total)

    rc, tail = runner(exe or "untrunc", ref_path, backup, _poll)
    result.output_tail = tail

    output = _find_untrunc_output(backups, work_name)
    if output is None or output.stat().st_size == 0:
        if output is not None:
            output.unlink(missing_ok=True)
        result.error = f"untrunc produced no output (exit {rc})"
        return result

    fixed = fixed_dir / work_name
    os.replace(output, fixed)

    _emit("verify", 0, 0)
    fixed_probe = probe_fn(fixed)
    if not fixed_probe.ok:
        # Success at the untrunc layer but the result is still undecodable —
        # remove it so a later accept can never swap garbage into the library.
        fixed.unlink(missing_ok=True)
        result.error = f"untrunc output is not decodable: {fixed_probe.error}"
        return result

    result.status = "ok"
    result.fixed_path = str(fixed)
    result.fixed_rel = pathing.library_rel_key(cfg.library_root, fixed)
    result.codec = fixed_probe.codec
    result.width = fixed_probe.width
    result.height = fixed_probe.height
    result.duration_s = fixed_probe.duration_s
    result.size = fixed.stat().st_size
    # A decodable-but-tiny output is untrunc giving up, not a repair: a mismatched
    # reference (different drone/settings) typically yields a sub-1% stub that
    # still passes ffprobe. Keep status 'ok' (the human verify step decides) but
    # say so loudly.
    ratio = result.size / total
    if ratio < 0.5:
        result.warning = (
            f"untrunc recovered only {ratio:.0%} of the broken file's data — the "
            "reference probably doesn't match (different drone or settings); "
            "discard and try another reference"
        )
    return result


# --------------------------------------------------------------------------- #
# Accept / discard / delete
# --------------------------------------------------------------------------- #


def _invalidate_rel(cfg, rel_key: str) -> None:
    cache_dir = cfg.cache_dir or config.default_cache_dir()
    invalidate_cache(cache_dir, config.resolve_proxy_cache_dir(cfg), rel_key)


def accept_repair(cfg, file_id: int, *, probe_fn=None) -> dict:
    """Swap the verified ``_repair/fixed/`` output onto the capture's dest path.

    Re-probes the output first (a stale or hand-damaged file must never replace
    library media), replaces the original in place (same volume — an atomic
    rename), refreshes the row's content fields (``sha256`` — favorites and
    duplicate detection key on it — plus codec/width/height/duration), and
    invalidates the derived cache so the placeholder poster regenerates from the
    repaired bytes. The pre-repair original REMAINS in ``_repair/backups/``.
    Raises :class:`ValueError` on an unknown id / missing or undecodable output.
    """
    probe_fn = probe_fn if probe_fn is not None else probe_file
    # integrity_check=True (the connect default): this mutates the index like
    # undo/retag/organize do — a corrupt index DB refuses the write.
    index = db.connect(cfg.index_db_path)
    try:
        db.init_index_schema(index)
        row = index.execute(
            "SELECT filename, dest_path FROM files WHERE id=?", (file_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown file id {file_id}")
        filename, dest_path = row

        _, fixed_dir = _repair_dirs(cfg)
        fixed = fixed_dir / _work_name(file_id, filename)
        if not fixed.is_file():
            raise ValueError("no repaired file is awaiting acceptance for this capture")
        probe = probe_fn(fixed)
        if not probe.ok:
            raise ValueError(f"repaired file failed verification: {probe.error}")

        sha = sha256_file(fixed)
        os.replace(fixed, _strip(dest_path))
        index.execute(
            "UPDATE files SET sha256=?, codec=?, width=?, height=?, duration_s=? "
            "WHERE id=?",
            (sha, probe.codec, probe.width, probe.height, probe.duration_s, file_id),
        )
        index.commit()
    finally:
        index.close()

    dest_rel = pathing.library_rel_key(cfg.library_root, dest_path)
    _invalidate_rel(cfg, dest_rel)  # the cached placeholder poster dies here
    _invalidate_rel(cfg, pathing.library_rel_key(cfg.library_root, fixed))
    return {
        "file_id": file_id,
        "path": dest_rel,
        "sha256": sha,
        "codec": probe.codec,
        "width": probe.width,
        "height": probe.height,
        "duration_s": probe.duration_s,
    }


def discard_repair(cfg, file_id: int) -> dict:
    """Drop an unaccepted repair attempt's work files (output + backup).

    The library original was never modified, so the backup copy is redundant once
    the output is rejected. Idempotent; raises :class:`ValueError` only for an
    unknown id.
    """
    index = db.connect(cfg.index_db_path, integrity_check=False)
    try:
        row = index.execute(
            "SELECT filename FROM files WHERE id=?", (file_id,)
        ).fetchone()
    finally:
        index.close()
    if row is None:
        raise ValueError(f"unknown file id {file_id}")

    backups, fixed_dir = _repair_dirs(cfg)
    work_name = _work_name(file_id, row[0])
    removed: list[str] = []
    stale = _find_untrunc_output(backups, work_name)
    for path in (fixed_dir / work_name, backups / work_name,
                 *((stale,) if stale is not None else ())):
        if path.is_file():
            path.unlink(missing_ok=True)
            removed.append(path.name)
    _invalidate_rel(cfg, pathing.library_rel_key(cfg.library_root,
                                                 fixed_dir / work_name))
    return {"file_id": file_id, "removed": removed}


def delete_broken(cfg, file_id: int, *, probe_fn=None) -> dict:
    """Delete a BROKEN capture from disk and prune its index rows.

    Server-side re-verification is the safety contract: the file is deleted only
    when it is currently missing, 0 bytes, or fails ffprobe — a file that probes
    healthy raises :class:`ValueError` and nothing is touched, so this endpoint
    can never delete good media no matter what the client sends. Companions
    (e.g. a stranded ``.SRT``) are deleted with their capture, any repair work
    files are cleaned up, and the index rows are pruned exactly like
    :mod:`geosorter.rescan` does.
    """
    probe_fn = probe_fn if probe_fn is not None else probe_file
    index = db.connect(cfg.index_db_path)
    try:
        db.init_index_schema(index)
        row = index.execute(
            "SELECT filename, media_type, dest_path, batch_id FROM files WHERE id=?",
            (file_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown file id {file_id}")
        filename, media_type, dest_path, batch_id = row
        companions = [
            r[0]
            for r in index.execute(
                "SELECT dest_path FROM file_companions WHERE primary_file_id=?",
                (file_id,),
            )
        ]

        path = Path(_strip(dest_path))
        if path.is_file():
            size = path.stat().st_size
            if size > 0:
                if media_type != "video":
                    raise ValueError(
                        "refusing to delete: only zero-byte photos are deletable here"
                    )
                probe = probe_fn(path)
                if probe.ok:
                    raise ValueError(
                        "refusing to delete: the file probes healthy on disk"
                    )

        deleted: list[str] = []
        for target in (path, *(Path(_strip(c)) for c in companions)):
            if target.is_file():
                target.unlink()
                deleted.append(target.name)
        rescan.prune_capture(index, file_id, dest_path, companions, batch_id)
    finally:
        index.close()

    backups, fixed_dir = _repair_dirs(cfg)
    work_name = _work_name(file_id, filename)
    stale = _find_untrunc_output(backups, work_name)
    for leftover in (backups / work_name, fixed_dir / work_name,
                     *((stale,) if stale is not None else ())):
        leftover.unlink(missing_ok=True)
    _invalidate_rel(cfg, pathing.library_rel_key(cfg.library_root, dest_path))
    return {"file_id": file_id, "deleted": deleted}
