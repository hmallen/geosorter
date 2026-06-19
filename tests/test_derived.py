"""Tests for lazy, cached derived-asset generation (thumbnails/posters/proxies).

The cache is tiered (m-cache-tiering-safety): callers pass an explicit ``cache_root``
(thumbs/posters/previews → a local SSD ``cache_dir``; proxies/stitch → ``proxy_cache_dir``)
and a ``rel_key`` (the source's library-relative path) so same-basename files in
different folders never collide. Each test supplies a tmp ``cache_root`` + ``rel_key``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from geosorter import derived

FIXTURES = Path(__file__).parent / "fixtures" / "media"
CACHE = ".geosorter-cache"


def _probe_codec(path: Path) -> str:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    return out.stdout.strip()


def test_thumbnail_creates_512px_jpeg(tmp_path):
    cache = tmp_path / "cache"
    out = derived.thumbnail(cache, "dji_photo.jpg", FIXTURES / "dji_photo.jpg")
    assert out.exists()
    with Image.open(out) as img:
        assert img.format == "JPEG"
        assert max(img.size) == 512  # 800x600 source -> 512x384
    assert out.is_relative_to(cache / CACHE / "thumbs")


def test_thumbnail_applies_exif_transpose(tmp_path):
    # A 100x50 image tagged Orientation=6 displays rotated to 50x100.
    src = tmp_path / "oriented.jpg"
    img = Image.new("RGB", (100, 50), "red")
    exif = img.getexif()
    exif[0x0112] = 6  # Orientation: rotate 90 CW on display
    img.save(src, exif=exif)

    out = derived.thumbnail(tmp_path / "cache", "oriented.jpg", src)
    with Image.open(out) as thumb:
        # exif_transpose must have swapped dimensions: height now exceeds width.
        assert thumb.size[1] > thumb.size[0]


def test_thumbnail_uses_jpeg_draft_for_fast_decode(tmp_path, monkeypatch):
    # draft('RGB', size) DCT-downscales the JPEG decode toward the target; it must
    # be invoked for a .jpg source (the fast-thumbnail win) and the output stays a
    # valid 512px JPEG. The decode object is a JpegImageFile, which overrides
    # draft(), so the spy goes on that subclass (the base Image.draft is unused here).
    from PIL import JpegImagePlugin

    calls = []
    orig = JpegImagePlugin.JpegImageFile.draft

    def spy(self, mode, size):
        calls.append((mode, size))
        return orig(self, mode, size)

    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "draft", spy)
    src = tmp_path / "big.jpg"
    Image.new("RGB", (2000, 1500), "red").save(src, "JPEG")
    out = derived.thumbnail(tmp_path / "cache", "big.jpg", src)
    # Our explicit draft uses mode 'RGB' (Pillow's own thumbnail() also calls
    # draft(None, ...), so filter by mode to isolate the one we added).
    assert any(mode == "RGB" for mode, _ in calls)
    with Image.open(out) as img:
        assert img.format == "JPEG"
        assert max(img.size) == 512


def test_thumbnail_skips_draft_for_non_jpeg(tmp_path, monkeypatch):
    # draft is JPEG-only; a PNG source must not route through our guarded draft('RGB').
    calls = []
    orig = Image.Image.draft
    monkeypatch.setattr(
        Image.Image, "draft",
        lambda self, m, s: calls.append((m, s)) or orig(self, m, s),
    )
    src = tmp_path / "shot.png"
    Image.new("RGB", (1200, 900), "blue").save(src, "PNG")
    out = derived.thumbnail(tmp_path / "cache", "shot.png", src)
    assert not any(mode == "RGB" for mode, _ in calls)  # no explicit 'RGB' draft
    with Image.open(out) as img:
        assert max(img.size) == 512


def test_thumbnail_cache_hit_does_not_regenerate(tmp_path):
    cache = tmp_path / "cache"
    out = derived.thumbnail(cache, "dji_photo.jpg", FIXTURES / "dji_photo.jpg")
    first = out.stat().st_mtime_ns
    again = derived.thumbnail(cache, "dji_photo.jpg", FIXTURES / "dji_photo.jpg")
    assert again == out
    assert again.stat().st_mtime_ns == first  # not regenerated


def test_same_basename_different_folders_no_collision(tmp_path):
    # The headline correctness fix: two DJI_0001.JPG in different folders must get
    # DISTINCT cache files (the old bare-filename fallback served the wrong thumbnail).
    cache = tmp_path / "cache"
    (tmp_path / "A").mkdir()
    (tmp_path / "B").mkdir()
    src_a = tmp_path / "A" / "DJI_0001.JPG"
    src_b = tmp_path / "B" / "DJI_0001.JPG"
    Image.new("RGB", (40, 30), "red").save(src_a)
    Image.new("RGB", (30, 40), "blue").save(src_b)

    out_a = derived.thumbnail(cache, "A/DJI_0001.JPG", src_a)
    out_b = derived.thumbnail(cache, "B/DJI_0001.JPG", src_b)
    assert out_a != out_b  # distinct cache files, no collision
    with Image.open(out_a) as ia, Image.open(out_b) as ib:
        assert ia.size[0] > ia.size[1]  # A is landscape
        assert ib.size[1] > ib.size[0]  # B is portrait — not A's content


def test_cache_freshness_pinned_to_source_mtime(tmp_path):
    # After generation the cache mtime is os.utime'd to the SOURCE mtime (not the
    # write time), so freshness is immune to SMB's coarse mtime granularity.
    cache = tmp_path / "cache"
    src = tmp_path / "photo.jpg"
    Image.new("RGB", (40, 30), "red").save(src)
    os.utime(src, (1_000_000.0, 1_000_000.0))  # a fixed, old source mtime

    out = derived.thumbnail(cache, "photo.jpg", src)
    assert out.stat().st_mtime == pytest.approx(1_000_000.0)  # pinned to the source


def test_poster_extracts_frame(tmp_path):
    out = derived.poster(tmp_path / "cache", "h264_tiny.mp4", FIXTURES / "h264_tiny.mp4")
    assert out.exists()
    with Image.open(out) as img:
        assert img.format == "JPEG"
        assert img.size == (320, 240)


def test_proxy_transcodes_hevc_to_h264(tmp_path):
    out = derived.proxy(tmp_path / "cache", "h265_tiny.mp4", FIXTURES / "h265_tiny.mp4", "h265")
    assert out.exists()
    assert out != FIXTURES / "h265_tiny.mp4"
    assert _probe_codec(out) == "h264"


def test_proxy_passthrough_for_h264(tmp_path):
    src = FIXTURES / "h264_tiny.mp4"
    out = derived.proxy(tmp_path / "cache", "h264_tiny.mp4", src, "h264")
    assert out == src  # no transcode for already-playable codecs


def test_proxy_cache_hit_does_not_regenerate(tmp_path):
    cache = tmp_path / "cache"
    out = derived.proxy(cache, "h265_tiny.mp4", FIXTURES / "h265_tiny.mp4", "h265")
    first = out.stat().st_mtime_ns
    again = derived.proxy(cache, "h265_tiny.mp4", FIXTURES / "h265_tiny.mp4", "h265")
    assert again.stat().st_mtime_ns == first


def test_proxy_lands_under_proxy_tier(tmp_path):
    # Proxies are written under the (SSD-SMB) proxy cache root, kind 'proxies'.
    proxy_root = tmp_path / "proxytier"
    out = derived.proxy(proxy_root, "clips/v.mp4", FIXTURES / "h265_tiny.mp4", "h265")
    assert out.is_relative_to(proxy_root / CACHE / "proxies")


# --- NVENC hardware-encoder selection (#124) -------------------------------- #
def _record_ffmpeg(monkeypatch, *, fail_on=None):
    """Monkeypatch ``derived._run_ffmpeg`` to record commands (no real transcode).

    Returns the list the recorded command lists are appended to. ``fail_on`` is a
    token substring: a recorded command containing it raises ``RuntimeError`` (an
    encoder failure) instead of writing the stub output, so a successful command writes
    a stub file at its output path (the last argument) and ``proxy``'s atomic replace
    succeeds.
    """
    calls: list[list[str]] = []

    def fake(cmd, **kwargs):
        calls.append(cmd)
        if fail_on is not None and any(fail_on in tok for tok in cmd):
            raise RuntimeError("simulated ffmpeg failure")
        Path(cmd[-1]).write_bytes(b"stub")

    monkeypatch.setattr(derived, "_run_ffmpeg", fake)
    return calls


def test_proxy_hwaccel_none_uses_libx264(tmp_path, monkeypatch):
    calls = _record_ffmpeg(monkeypatch)
    out = derived.proxy(tmp_path / "c", "v.mp4", FIXTURES / "h265_tiny.mp4", "h265",
                        hwaccel="none")
    assert out.exists()
    assert len(calls) == 1
    assert "libx264" in calls[0] and "h264_nvenc" not in calls[0]


def test_proxy_hwaccel_nvenc_builds_gpu_command(tmp_path, monkeypatch):
    calls = _record_ffmpeg(monkeypatch)
    derived.proxy(tmp_path / "c", "v.mp4", FIXTURES / "h265_tiny.mp4", "h265",
                  hwaccel="nvenc")
    assert len(calls) == 1
    cmd = calls[0]
    assert "h264_nvenc" in cmd
    assert "scale_cuda=format=yuv420p" in cmd  # 10-bit Main10 -> 8-bit
    assert "23" in cmd and "-cq" in cmd        # constant-quality knob
    assert "cuda" in cmd                        # full GPU decode pipeline


def test_proxy_auto_uses_nvenc_when_detected(tmp_path, monkeypatch):
    monkeypatch.setattr(derived, "_detect_nvenc", lambda: True)
    calls = _record_ffmpeg(monkeypatch)
    derived.proxy(tmp_path / "c", "v.mp4", FIXTURES / "h265_tiny.mp4", "h265",
                  hwaccel="auto")
    assert len(calls) == 1 and "h264_nvenc" in calls[0]


def test_proxy_auto_uses_libx264_when_not_detected(tmp_path, monkeypatch):
    monkeypatch.setattr(derived, "_detect_nvenc", lambda: False)
    calls = _record_ffmpeg(monkeypatch)
    derived.proxy(tmp_path / "c", "v.mp4", FIXTURES / "h265_tiny.mp4", "h265",
                  hwaccel="auto")
    assert len(calls) == 1 and "libx264" in calls[0]


def test_proxy_auto_falls_back_to_libx264_on_nvenc_failure(tmp_path, monkeypatch):
    # NVENC is detected and attempted, fails, and 'auto' retries with libx264.
    monkeypatch.setattr(derived, "_detect_nvenc", lambda: True)
    calls = _record_ffmpeg(monkeypatch, fail_on="h264_nvenc")
    out = derived.proxy(tmp_path / "c", "v.mp4", FIXTURES / "h265_tiny.mp4", "h265",
                        hwaccel="auto")
    assert out.exists()
    assert len(calls) == 2
    assert "h264_nvenc" in calls[0]   # tried NVENC first
    assert "libx264" in calls[1]      # fell back to CPU


def test_proxy_nvenc_strict_raises_on_failure(tmp_path, monkeypatch):
    # Explicit 'nvenc' is strict: a failure surfaces and is NOT retried with libx264.
    calls = _record_ffmpeg(monkeypatch, fail_on="h264_nvenc")
    with pytest.raises(RuntimeError):
        derived.proxy(tmp_path / "c", "v.mp4", FIXTURES / "h265_tiny.mp4", "h265",
                      hwaccel="nvenc")
    assert len(calls) == 1 and "h264_nvenc" in calls[0]  # no libx264 fallback


def test_proxy_passthrough_ignores_hwaccel(tmp_path, monkeypatch):
    # A non-HEVC source is returned unchanged regardless of hwaccel — no transcode.
    calls = _record_ffmpeg(monkeypatch)
    src = FIXTURES / "h264_tiny.mp4"
    out = derived.proxy(tmp_path / "c", "v.mp4", src, "h264", hwaccel="nvenc")
    assert out == src and calls == []


def test_detect_nvenc_returns_false_when_ffmpeg_absent(monkeypatch):
    derived._detect_nvenc.cache_clear()

    def boom(*a, **k):
        raise FileNotFoundError("no ffmpeg")

    monkeypatch.setattr(derived.subprocess, "run", boom)
    assert derived._detect_nvenc() is False
    derived._detect_nvenc.cache_clear()


# --- verbose ffmpeg (warm-proxies --show-ffmpeg) ----------------------------- #
def _has_subseq(cmd: list[str], sub: list[str]) -> bool:
    """True if ``sub`` appears as a contiguous subsequence of ``cmd``."""
    n = len(sub)
    return any(cmd[i:i + n] == sub for i in range(len(cmd) - n + 1))


@pytest.mark.parametrize("nvenc", [False, True])
def test_proxy_cmd_quiet_by_default(nvenc):
    cmd = derived._proxy_cmd(Path("s.mp4"), Path("d.mp4"), nvenc=nvenc)
    assert _has_subseq(cmd, ["-v", "error"])  # suppressed output (today's behaviour)


@pytest.mark.parametrize("nvenc", [False, True])
def test_proxy_cmd_verbose_drops_quiet_flag(nvenc):
    cmd = derived._proxy_cmd(Path("s.mp4"), Path("d.mp4"), nvenc=nvenc, verbose=True)
    assert not _has_subseq(cmd, ["-v", "error"])  # full ffmpeg output
    assert cmd[0] == "ffmpeg" and str(Path("d.mp4")) == cmd[-1]  # otherwise intact


def test_proxy_threads_verbose_to_helpers(tmp_path, monkeypatch):
    seen: list[bool] = []

    def fake(cmd, **kwargs):
        seen.append(kwargs.get("verbose", False))
        Path(cmd[-1]).write_bytes(b"stub")

    monkeypatch.setattr(derived, "_run_ffmpeg", fake)
    derived.proxy(tmp_path / "c", "v.mp4", FIXTURES / "h265_tiny.mp4", "h265",
                  hwaccel="none", verbose=True)
    assert seen == [True]  # _run_ffmpeg called with verbose=True


def test_proxy_default_is_not_verbose(tmp_path, monkeypatch):
    seen: list[bool] = []

    def fake(cmd, **kwargs):
        seen.append(kwargs.get("verbose", False))
        Path(cmd[-1]).write_bytes(b"stub")

    monkeypatch.setattr(derived, "_run_ffmpeg", fake)
    derived.proxy(tmp_path / "c", "v.mp4", FIXTURES / "h265_tiny.mp4", "h265",
                  hwaccel="none")
    assert seen == [False]


def test_run_ffmpeg_verbose_does_not_capture(monkeypatch):
    captured: list[dict] = []

    def fake_run(cmd, **kwargs):
        captured.append(kwargs)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(derived.subprocess, "run", fake_run)
    derived._run_ffmpeg(["ffmpeg", "x"], verbose=True)
    assert captured[0].get("capture_output") is not True  # output inherits the terminal


def test_run_ffmpeg_verbose_failure_raises_without_stderr(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1)  # non-zero, no captured stderr

    monkeypatch.setattr(derived.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        derived._run_ffmpeg(["ffmpeg", "x"], verbose=True)


def test_preview_caps_long_edge_at_1920(tmp_path):
    cache = tmp_path / "cache"
    src = tmp_path / "big.jpg"
    Image.new("RGB", (4000, 3000), "red").save(src)
    out = derived.preview(cache, "big.jpg", src)
    assert out.exists()
    with Image.open(out) as img:
        assert img.format == "JPEG"
        assert max(img.size) == 1920  # 4000x3000 -> 1920x1440
    assert out.is_relative_to(cache / CACHE / "previews")


def test_preview_passthrough_small(tmp_path):
    # An 800px source is already under the 1920 cap; no upscale.
    out = derived.preview(tmp_path / "cache", "dji_photo.jpg", FIXTURES / "dji_photo.jpg")
    with Image.open(out) as img:
        assert max(img.size) == 800


def test_run_ffmpeg_wraps_timeout_as_runtimeerror(monkeypatch):
    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1)

    monkeypatch.setattr(derived.subprocess, "run", _timeout)
    with pytest.raises(RuntimeError):
        derived._run_ffmpeg(["ffmpeg", "x"], timeout=1)


def test_run_ffmpeg_timeout_leaves_no_partial(tmp_path, monkeypatch):
    # A timed-out transcode raises and _atomic_write removes the temp (no half file).
    out = tmp_path / "cache" / CACHE / "proxies" / "v.mp4"

    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1)

    monkeypatch.setattr(derived.subprocess, "run", _timeout)
    with pytest.raises(RuntimeError):
        derived._atomic_write(out, lambda dest: derived._run_ffmpeg(["ffmpeg", str(dest)]))
    assert not out.exists()
    assert not (out.parent.exists() and list(out.parent.iterdir()))


def test_find_hugin_returns_none_when_any_tool_missing(monkeypatch):
    monkeypatch.setattr(derived.shutil, "which", lambda name: None)
    assert derived.find_hugin() is None


def test_find_hugin_returns_all_tools_on_path(monkeypatch):
    monkeypatch.setattr(derived.shutil, "which", lambda name: f"/usr/bin/{Path(name).name}")
    tools = derived.find_hugin()
    assert tools is not None
    assert set(tools) == set(derived._HUGIN_TOOLS)


def test_find_hugin_uses_bin_dir(monkeypatch, tmp_path):
    seen: list[str] = []

    def _which(name):
        seen.append(name)
        return name  # pretend each lookup resolves

    monkeypatch.setattr(derived.shutil, "which", _which)
    bindir = tmp_path / "hbin"
    tools = derived.find_hugin(bindir)
    assert tools is not None
    assert all(str(bindir) in s for s in seen)  # looked under the bin dir, not bare PATH


def test_run_hugin_raises_stitchfailed_on_nonzero(monkeypatch):
    class _Result:
        returncode = 1
        stderr = "control points lost"

    monkeypatch.setattr(derived.subprocess, "run", lambda *a, **k: _Result())
    with pytest.raises(derived.StitchFailed):
        derived._run_hugin(["cpfind", "x"])


def test_run_hugin_wraps_timeout_as_stitchfailed(monkeypatch):
    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="cpfind", timeout=1)

    monkeypatch.setattr(derived.subprocess, "run", _timeout)
    with pytest.raises(derived.StitchFailed):
        derived._run_hugin(["cpfind", "x"], timeout=1)


def _make_pano_tiles(library_root: Path, n: int = 3):
    """Create a primary tile + (n-1) frame tiles under library_root/pano/."""
    d = library_root / "pano"
    d.mkdir(parents=True)
    primary = d / "PANO_0001.JPG"
    Image.new("RGB", (400, 300), "green").save(primary)
    frames = []
    for i in range(2, n + 1):
        f = d / f"PANO_{i:04d}.JPG"
        Image.new("RGB", (400, 300), "green").save(f)
        frames.append(f)
    return primary, frames


def _fake_hugin_tools(hugin_bin_dir=None):
    return {name: name for name in derived._HUGIN_TOOLS}


def _fake_run_factory(
    fill: str = "skyblue", size: tuple[int, int] = (3000, 1400), hfov: float = 360.0
):
    """A fake _run_hugin that drives the projection-aware pipeline.

    On the ``autooptimiser`` step it writes an optimised ``.pto`` whose ``p`` line
    carries ``hfov`` (so ``panorama_stitch`` can read the HFOV back and pick the
    projection); on the ``--stitching`` step it emits a known ``out.tif``. Default
    ``hfov=360`` + a 2.14:1 ``size`` keeps the equirectangular path (the prior
    behaviour) so the existing assertions hold."""
    calls: list[list[str]] = []

    def _run(cmd, *, timeout: int = derived.STITCH_STEP_TIMEOUT_S):
        calls.append(cmd)
        if Path(cmd[0]).name == "autooptimiser":
            pto = cmd[cmd.index("-o") + 1]
            Path(pto).write_text(f'p f2 w6000 h3000 v{hfov} n"TIFF_m"\n')
        if "--stitching" in cmd:
            prefix = next(a.split("=", 1)[1] for a in cmd if a.startswith("--prefix="))
            Image.new("RGB", size, fill).save(prefix + ".tif")

    return _run, calls


def test_panorama_stitch_success_caches_and_is_idempotent(tmp_path, monkeypatch):
    library_root = tmp_path / "lib"
    primary, frames = _make_pano_tiles(library_root)
    cache = tmp_path / "proxytier"
    monkeypatch.setattr(derived, "find_hugin", _fake_hugin_tools)
    run, calls = _fake_run_factory()
    monkeypatch.setattr(derived, "_run_hugin", run)

    result = derived.panorama_stitch(cache, "pano/PANO_0001.JPG", primary, frames)
    out = result.path
    assert out.exists()
    assert result.projection == "equirectangular"  # default fake HFOV 360 -> full sphere
    with Image.open(out) as img:
        assert img.format == "JPEG"
    assert out.is_relative_to(cache / CACHE / "stitch")
    first_calls = len(calls)
    assert first_calls >= 6  # the full 6-step pipeline ran

    result2 = derived.panorama_stitch(cache, "pano/PANO_0001.JPG", primary, frames)
    assert result2.path == out
    assert result2.projection == ""  # cache hit -> no new projection info
    assert len(calls) == first_calls  # cache hit -> no further hugin invocations


def test_panorama_stitch_force_bypasses_fresh_cache(tmp_path, monkeypatch):
    # force=True re-runs the pipeline cold even when the cached hero is fresh, so a
    # hero baked at the old hard-coded projection can be regenerated with the new
    # auto-detect; force=False (default) keeps the freshness cache-hit short-circuit.
    library_root = tmp_path / "lib"
    primary, frames = _make_pano_tiles(library_root)
    cache = tmp_path / "proxytier"
    monkeypatch.setattr(derived, "find_hugin", _fake_hugin_tools)
    run, calls = _fake_run_factory()
    monkeypatch.setattr(derived, "_run_hugin", run)

    first = derived.panorama_stitch(cache, "pano/PANO_0001.JPG", primary, frames)
    assert first.path.exists()
    after_first = len(calls)

    # default (force=False): a fresh cache hit, no further Hugin work
    hit = derived.panorama_stitch(cache, "pano/PANO_0001.JPG", primary, frames)
    assert hit.projection == ""
    assert len(calls) == after_first

    # force=True: runs the pipeline cold again and returns the real projection
    forced = derived.panorama_stitch(
        cache, "pano/PANO_0001.JPG", primary, frames, force=True
    )
    assert forced.projection == "equirectangular"
    assert len(calls) > after_first


def test_panorama_stitch_force_failure_preserves_existing_hero(tmp_path, monkeypatch):
    # A forced re-stitch that FAILS must not destroy the already-cached hero: the
    # gate/Hugin failures raise before _atomic_write replaces the cache file.
    library_root = tmp_path / "lib"
    primary, frames = _make_pano_tiles(library_root)
    cache = tmp_path / "proxytier"
    monkeypatch.setattr(derived, "find_hugin", _fake_hugin_tools)
    run, _ = _fake_run_factory()
    monkeypatch.setattr(derived, "_run_hugin", run)
    out = derived.panorama_stitch(cache, "pano/PANO_0001.JPG", primary, frames).path
    original = out.read_bytes()

    def _boom(cmd, *, timeout: int = derived.STITCH_STEP_TIMEOUT_S):
        raise derived.StitchFailed("cpfind failed")

    monkeypatch.setattr(derived, "_run_hugin", _boom)
    with pytest.raises(derived.StitchFailed):
        derived.panorama_stitch(cache, "pano/PANO_0001.JPG", primary, frames, force=True)
    assert out.exists()
    assert out.read_bytes() == original  # untouched on failure


def test_forced_projection_code_maps_each_projection():
    # The three manual-override projections map to (Hugin code, stored family).
    assert derived._forced_projection_code("equirectangular") == (
        derived._PROJ_EQUIRECTANGULAR, "equirectangular"
    )
    assert derived._forced_projection_code("cylindrical") == (
        derived._PROJ_CYLINDRICAL, "flat"
    )
    assert derived._forced_projection_code("rectilinear") == (
        derived._PROJ_RECTILINEAR, "flat"
    )
    with pytest.raises(derived.StitchFailed):
        derived._forced_projection_code("bogus")


def test_panorama_stitch_forced_projection_overrides_autodetect(tmp_path, monkeypatch):
    # forced_projection wins over the HFOV-derived choice: the default fake HFOV 360
    # would auto-pick equirectangular, but forcing 'cylindrical' records 'flat' and
    # feeds the cylindrical code (1) to pano_modify --projection= (and the one-way
    # equirectangular->flat reclassify is skipped, honouring the explicit choice).
    library_root = tmp_path / "lib"
    primary, frames = _make_pano_tiles(library_root)
    cache = tmp_path / "proxytier"
    monkeypatch.setattr(derived, "find_hugin", _fake_hugin_tools)
    run, calls = _fake_run_factory()  # hfov 360 -> auto equirectangular
    monkeypatch.setattr(derived, "_run_hugin", run)

    result = derived.panorama_stitch(
        cache, "pano/PANO_0001.JPG", primary, frames, forced_projection="cylindrical"
    )
    assert result.projection == "flat"
    pano_modify = next(c for c in calls if Path(c[0]).name == "pano_modify")
    assert f"--projection={derived._PROJ_CYLINDRICAL}" in pano_modify


def test_panorama_stitch_reports_steps(tmp_path, monkeypatch):
    # The on_step callback fires once per Hugin pipeline step, in order, so the
    # background job (and the UI) can show which of the six steps is running.
    library_root = tmp_path / "lib"
    primary, frames = _make_pano_tiles(library_root)
    monkeypatch.setattr(derived, "find_hugin", _fake_hugin_tools)
    run, _calls = _fake_run_factory()
    monkeypatch.setattr(derived, "_run_hugin", run)

    steps: list[tuple[int, int, str]] = []
    derived.panorama_stitch(
        tmp_path / "proxytier", "pano/PANO_0001.JPG", primary, frames,
        on_step=lambda i, n, name: steps.append((i, n, name)),
    )

    assert [name for _i, _n, name in steps] == [
        "pto_gen", "cpfind", "cpclean", "autooptimiser", "pano_modify", "hugin_executor",
    ]
    assert [i for i, _n, _name in steps] == [1, 2, 3, 4, 5, 6]
    assert all(n == 6 for _i, n, _name in steps)


def test_panorama_stitch_missing_hugin_raises(tmp_path, monkeypatch):
    library_root = tmp_path / "lib"
    primary, frames = _make_pano_tiles(library_root)
    monkeypatch.setattr(derived, "find_hugin", lambda hugin_bin_dir=None: None)
    with pytest.raises(derived.HuginNotFound):
        derived.panorama_stitch(tmp_path / "proxytier", "pano/PANO_0001.JPG", primary, frames)


def test_panorama_stitch_pipeline_failure_raises_and_caches_nothing(tmp_path, monkeypatch):
    library_root = tmp_path / "lib"
    primary, frames = _make_pano_tiles(library_root)
    cache = tmp_path / "proxytier"
    monkeypatch.setattr(derived, "find_hugin", _fake_hugin_tools)

    def _boom(cmd, *, timeout: int = derived.STITCH_STEP_TIMEOUT_S):
        raise derived.StitchFailed("cpfind failed")

    monkeypatch.setattr(derived, "_run_hugin", _boom)
    with pytest.raises(derived.StitchFailed):
        derived.panorama_stitch(cache, "pano/PANO_0001.JPG", primary, frames)
    stitch_cache = cache / CACHE / "stitch"
    assert not (stitch_cache.exists() and list(stitch_cache.rglob("*.jpg")))


def test_panorama_stitch_rejects_degenerate_black_output(tmp_path, monkeypatch):
    library_root = tmp_path / "lib"
    primary, frames = _make_pano_tiles(library_root)
    monkeypatch.setattr(derived, "find_hugin", _fake_hugin_tools)
    run, _ = _fake_run_factory(fill="black")  # ~100% black void
    monkeypatch.setattr(derived, "_run_hugin", run)
    with pytest.raises(derived.StitchFailed):
        derived.panorama_stitch(tmp_path / "proxytier", "pano/PANO_0001.JPG", primary, frames)


def test_panorama_stitch_real_hugin_e2e(tmp_path):
    # End-to-end against the REAL Hugin pipeline + real DJI tiles. Skips unless both
    # Hugin is installed AND a local panorama tile set is staged (mirrors the
    # committed-media e2e convention) — so the default suite stays fast and green.
    if derived.find_hugin() is None:
        pytest.skip("Hugin CLI not found on PATH / hugin_bin_dir")
    pano_dir = FIXTURES / "local" / "panorama"
    tiles = sorted(pano_dir.glob("PANO_*.JPG")) if pano_dir.is_dir() else []
    if len(tiles) < 2:
        pytest.skip(f"no local panorama tiles at {pano_dir}")

    result = derived.panorama_stitch(tmp_path, "PANO_0001.JPG", tiles[0], tiles[1:])
    assert result.path.is_file()
    assert result.projection in ("equirectangular", "flat")
    derived._stitch_gate(result.path, result.projection)  # real output passes its gate


# --- Stitch step opt-out + canvas config (m-frontend-pano-ux) -------------- #


def test_panorama_stitch_drops_celeste_and_lens_when_disabled(tmp_path, monkeypatch):
    library_root = tmp_path / "lib"
    primary, frames = _make_pano_tiles(library_root)
    monkeypatch.setattr(derived, "find_hugin", _fake_hugin_tools)
    run, calls = _fake_run_factory()
    monkeypatch.setattr(derived, "_run_hugin", run)

    derived.panorama_stitch(
        tmp_path / "c", "pano/PANO_0001.JPG", primary, frames,
        canvas="4000x2000", celeste=False, optimise_lens=False,
    )
    cpfind = next(c for c in calls if Path(c[0]).name == "cpfind")
    assert "--celeste" not in cpfind
    autoopt = next(c for c in calls if Path(c[0]).name == "autooptimiser")
    assert "-l" not in autoopt
    pano_modify = next(c for c in calls if Path(c[0]).name == "pano_modify")
    assert "--canvas=4000x2000" in pano_modify


def test_panorama_stitch_keeps_celeste_and_lens_by_default(tmp_path, monkeypatch):
    library_root = tmp_path / "lib"
    primary, frames = _make_pano_tiles(library_root)
    monkeypatch.setattr(derived, "find_hugin", _fake_hugin_tools)
    run, calls = _fake_run_factory()
    monkeypatch.setattr(derived, "_run_hugin", run)

    derived.panorama_stitch(tmp_path / "c", "pano/PANO_0001.JPG", primary, frames)
    cpfind = next(c for c in calls if Path(c[0]).name == "cpfind")
    assert "--celeste" in cpfind
    autoopt = next(c for c in calls if Path(c[0]).name == "autooptimiser")
    assert "-l" in autoopt
    # The default canvas shrank to 4000x2000 (m-frontend-pano-ux).
    pano_modify = next(c for c in calls if Path(c[0]).name == "pano_modify")
    assert "--canvas=4000x2000" in pano_modify


# --- Projection auto-detection (m-fix-panorama-projection-autodetect) ------ #


def test_parse_pto_hfov_reads_p_line_v_token():
    pto = (
        'p f2 w6000 h3000 v360 E0 R0 n"TIFF_m c:LZW"\n'
        'm g1 i0 f0 m2 p0.00784314\n'
        'i w4032 h3024 f0 v78.8 ...\n'  # an image line (its own v) must NOT win
    )
    assert derived._parse_pto_hfov(pto) == 360.0
    assert derived._parse_pto_hfov('p f1 w4000 h1400 v149.5 n"TIFF_m"\n') == 149.5


def test_parse_pto_hfov_missing_p_line_raises():
    with pytest.raises(derived.StitchFailed):
        derived._parse_pto_hfov('m g1 i0\ni w4032 h3024 v78.8\n')  # no p line


def test_parse_pto_hfov_malformed_token_raises():
    # The loose [\d.]+ regex can admit a token float() rejects (e.g. v3.6.0); the
    # docstring promises StitchFailed for a malformed project, not a bare ValueError.
    with pytest.raises(derived.StitchFailed):
        derived._parse_pto_hfov('p f2 w6000 h3000 v3.6.0 n"x"\n')


def test_classify_stitched_projection_by_aspect(tmp_path):
    eq = tmp_path / "eq.jpg"
    Image.new("RGB", (4000, 2000), "skyblue").save(eq)  # 2:1 -> equirectangular
    assert derived.classify_stitched_projection(eq) == "equirectangular"
    flat = tmp_path / "flat.jpg"
    Image.new("RGB", (4000, 743), "skyblue").save(flat)  # 5.38:1 -> flat
    assert derived.classify_stitched_projection(flat) == "flat"


def test_choose_projection_by_hfov():
    assert derived._choose_projection(360.0) == (2, "equirectangular")
    assert derived._choose_projection(280.0) == (2, "equirectangular")
    assert derived._choose_projection(270.0) == (2, "equirectangular")
    assert derived._choose_projection(200.0) == (1, "flat")
    assert derived._choose_projection(120.0) == (1, "flat")
    assert derived._choose_projection(119.9) == (0, "flat")
    assert derived._choose_projection(90.0) == (0, "flat")


def test_stitch_gate_is_projection_aware(tmp_path):
    # A 4000x743 (~5.38:1) wide pano — the file_id 14 failure shape. It is rejected
    # as equirectangular (out of [1.3, 3.0]) but ACCEPTED as a flat hero.
    wide = tmp_path / "wide.tif"
    Image.new("RGB", (4000, 743), "skyblue").save(wide)
    with pytest.raises(derived.StitchFailed):
        derived._stitch_gate(wide, "equirectangular")
    derived._stitch_gate(wide, "flat")  # passes — no raise

    # A 4000x2000 (2:1) equirectangular result passes the equirectangular gate.
    sphere = tmp_path / "sphere.tif"
    Image.new("RGB", (4000, 2000), "skyblue").save(sphere)
    derived._stitch_gate(sphere, "equirectangular")

    # A tall 743x4000 (~0.19) vertical pano: flat's lower bound is 0.2, so this is
    # rejected even as flat (degenerate sliver); a 800x4000 (0.2) tall pano passes.
    tall_ok = tmp_path / "tall.tif"
    Image.new("RGB", (800, 4000), "skyblue").save(tall_ok)
    derived._stitch_gate(tall_ok, "flat")

    # Black-void guard is projection-independent: rejected for BOTH kinds.
    void = tmp_path / "void.tif"
    Image.new("RGB", (4000, 2000), "black").save(void)
    with pytest.raises(derived.StitchFailed):
        derived._stitch_gate(void, "equirectangular")
    with pytest.raises(derived.StitchFailed):
        derived._stitch_gate(void, "flat")


def test_panorama_stitch_flat_projection_for_narrow_pano(tmp_path, monkeypatch):
    # A narrow-FOV pano (HFOV 149) must stitch as a flat hero: pano_modify gets the
    # cylindrical projection (code 1), the wide 5.38:1 output passes the flat gate, and
    # the StitchResult reports projection='flat' (regression for the file_id 14 reject).
    library_root = tmp_path / "lib"
    primary, frames = _make_pano_tiles(library_root)
    monkeypatch.setattr(derived, "find_hugin", _fake_hugin_tools)
    run, calls = _fake_run_factory(size=(4000, 743), hfov=149.0)
    monkeypatch.setattr(derived, "_run_hugin", run)

    result = derived.panorama_stitch(tmp_path / "c", "pano/PANO_0001.JPG", primary, frames)
    assert result.projection == "flat"
    pano_modify = next(c for c in calls if Path(c[0]).name == "pano_modify")
    assert "--projection=1" in pano_modify  # cylindrical, not the old hard-coded =2


def test_panorama_stitch_equirect_projection_for_full_sphere(tmp_path, monkeypatch):
    # A full sphere (HFOV 360) keeps equirectangular (code 2) — no regression.
    library_root = tmp_path / "lib"
    primary, frames = _make_pano_tiles(library_root)
    monkeypatch.setattr(derived, "find_hugin", _fake_hugin_tools)
    run, calls = _fake_run_factory(size=(4000, 2000), hfov=360.0)
    monkeypatch.setattr(derived, "_run_hugin", run)

    result = derived.panorama_stitch(tmp_path / "c", "pano/PANO_0001.JPG", primary, frames)
    assert result.projection == "equirectangular"
    pano_modify = next(c for c in calls if Path(c[0]).name == "pano_modify")
    assert "--projection=2" in pano_modify


def test_panorama_stitch_wide_360_reclassifies_to_flat(tmp_path, monkeypatch):
    # A single-row 360 sweep: HFOV 360 makes _choose_projection pick equirectangular,
    # but --crop=AUTO yields a wide ~8:1 strip. That is a legitimate flat panning image,
    # not a 2:1 sphere — panorama_stitch must reclassify it to 'flat' (reusing the
    # rendered output) instead of rejecting it as out-of-range equirectangular.
    library_root = tmp_path / "lib"
    primary, frames = _make_pano_tiles(library_root)
    monkeypatch.setattr(derived, "find_hugin", _fake_hugin_tools)
    run, _ = _fake_run_factory(size=(4000, 490), hfov=360.0)  # aspect ~8.16
    monkeypatch.setattr(derived, "_run_hugin", run)

    result = derived.panorama_stitch(tmp_path / "c", "pano/PANO_0001.JPG", primary, frames)
    assert result.projection == "flat"
    assert result.path.exists()


def test_panorama_stitch_wide_black_output_still_fails(tmp_path, monkeypatch):
    # The reclassification only relaxes the ASPECT envelope; a wide but mostly-black
    # (degenerate) output is still rejected by the projection-independent black-void guard.
    library_root = tmp_path / "lib"
    primary, frames = _make_pano_tiles(library_root)
    monkeypatch.setattr(derived, "find_hugin", _fake_hugin_tools)
    run, _ = _fake_run_factory(fill="black", size=(4000, 490), hfov=360.0)
    monkeypatch.setattr(derived, "_run_hugin", run)
    with pytest.raises(derived.StitchFailed):
        derived.panorama_stitch(tmp_path / "c", "pano/PANO_0001.JPG", primary, frames)


# --- Instant raw-tile collage (m-frontend-pano-ux) ------------------------- #


def test_panorama_collage_composes_tiles_and_is_idempotent(tmp_path):
    library_root = tmp_path / "lib"
    primary, frames = _make_pano_tiles(library_root, n=4)
    cache = tmp_path / "localtier"  # collage lives on the LOCAL cache tier

    out = derived.panorama_collage(cache, "pano/PANO_0001.JPG", primary, frames)
    assert out.is_file()
    assert out.suffix == ".jpg"
    # Cached under the local tier's "collage" kind, keyed by rel_key.
    assert out.is_relative_to(cache / derived.CACHE_DIRNAME / "collage")
    with Image.open(out) as im:
        w, h = im.size
    assert w >= 400 and h >= 300  # a multi-tile grid, bigger than one 400x300 cell

    # Idempotent: a re-call finds the fresh file (mtime pinned to the newest tile)
    # and returns it without rewriting.
    mtime1 = out.stat().st_mtime
    out2 = derived.panorama_collage(cache, "pano/PANO_0001.JPG", primary, frames)
    assert out2 == out
    assert out2.stat().st_mtime == mtime1


def test_panorama_collage_tolerates_unreadable_tile(tmp_path):
    library_root = tmp_path / "lib"
    primary, frames = _make_pano_tiles(library_root, n=3)
    frames[0].write_bytes(b"not a real image")  # corrupt one tile

    cache = tmp_path / "localtier"
    out = derived.panorama_collage(cache, "pano/PANO_0001.JPG", primary, frames)
    assert out.is_file()  # the bad tile leaves a black cell, never aborts


def test_panorama_collage_tolerates_missing_tile(tmp_path):
    # A panorama_frame companion can be gone on disk (rescan keeps that state). The
    # freshness max() must not raise FileNotFoundError before the per-tile fallback —
    # the collage still serves a degraded placeholder (regression for the 500 a bare
    # max(t.stat()...) over a missing tile would have caused).
    library_root = tmp_path / "lib"
    primary, frames = _make_pano_tiles(library_root, n=3)
    frames[0].unlink()  # delete one tile entirely

    cache = tmp_path / "localtier"
    out = derived.panorama_collage(cache, "pano/PANO_0001.JPG", primary, frames)
    assert out.is_file()


def test_collage_is_a_local_evictable_kind():
    # The collage is small/hot/regenerable, so it lives on the local tier and the
    # atime-LRU sweep manages it (unlike the write-once stitch on the proxy tier).
    assert "collage" in derived._LOCAL_CACHE_KINDS


# --- Generation concurrency cap (m-derived-at-scale) ----------------------- #


def test_generation_concurrency_capped(tmp_path, monkeypatch):
    # A cold-browse storm must not run unbounded Pillow/ffmpeg generations at once:
    # the module-level semaphore caps concurrent generation. A slow _atomic_write
    # records the peak in-flight count across many parallel thumbnail() calls.
    import threading
    import time as _time

    lock = threading.Lock()
    state = {"cur": 0, "peak": 0}

    def slow_atomic(out, produce):
        with lock:
            state["cur"] += 1
            state["peak"] = max(state["peak"], state["cur"])
        _time.sleep(0.05)
        with lock:
            state["cur"] -= 1

    monkeypatch.setattr(derived, "_atomic_write", slow_atomic)
    src = tmp_path / "s.jpg"
    Image.new("RGB", (64, 64), "red").save(src, "JPEG")
    cache = tmp_path / "cache"
    n = derived.DERIVED_MAX_CONCURRENCY + 4
    # Distinct rel_keys -> distinct cache files (no freshness-skip collisions).
    threads = [
        threading.Thread(target=derived.thumbnail, args=(cache, f"k{i}.jpg", src))
        for i in range(n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["peak"] <= derived.DERIVED_MAX_CONCURRENCY
    assert state["peak"] >= 2  # sanity: genuinely ran in parallel up to the cap


# --- Local-tier cache eviction (m-derived-at-scale) ------------------------ #


def _make_cache_file(base: Path, name: str, size: int, atime: float) -> Path:
    f = base / name
    f.write_bytes(b"\0" * size)
    os.utime(f, (atime, atime))  # pin atime (the eviction key) AND mtime
    return f


def test_evict_local_cache_drops_oldest_over_cap(tmp_path):
    cache_root = tmp_path / "cache"
    thumbs = cache_root / CACHE / "thumbs"
    thumbs.mkdir(parents=True)
    mib = 1 << 20
    files = [_make_cache_file(thumbs, f"f{i}.jpg", mib, atime=1000 + i) for i in range(5)]

    res = derived.evict_local_cache(cache_root, max_gb=3 / 1024)  # 3 MiB cap

    assert not files[0].exists() and not files[1].exists()  # 2 oldest atimes dropped
    assert all(f.exists() for f in files[2:])  # newest 3 kept
    assert res.deleted == 2
    assert res.skipped == 0
    assert res.bytes_after <= 3 * mib


def test_evict_local_cache_defers_on_permissionerror(tmp_path, monkeypatch):
    cache_root = tmp_path / "cache"
    thumbs = cache_root / CACHE / "thumbs"
    thumbs.mkdir(parents=True)
    mib = 1 << 20
    files = [_make_cache_file(thumbs, f"f{i}.jpg", mib, atime=1000 + i) for i in range(3)]

    real_unlink = Path.unlink

    def guarded_unlink(self, *a, **k):
        if self.name == "f0.jpg":  # the oldest (would be evicted first) is "open"
            raise PermissionError("file in use")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", guarded_unlink)
    res = derived.evict_local_cache(cache_root, max_gb=1.5 / 1024)  # 1.5 MiB cap

    assert files[0].exists()  # locked oldest skipped, not crashed
    assert res.skipped == 1
    assert not files[1].exists()  # eviction continued past the locked file
    assert res.deleted == 2


def test_evict_local_cache_tolerates_vanished_file(tmp_path, monkeypatch):
    # A concurrent _atomic_write temp can be renamed away between rglob listing and
    # stat(); the sweep must skip it, not abort with FileNotFoundError.
    cache_root = tmp_path / "cache"
    thumbs = cache_root / CACHE / "thumbs"
    thumbs.mkdir(parents=True)
    mib = 1 << 20
    files = [_make_cache_file(thumbs, f"f{i}.jpg", mib, atime=1000 + i) for i in range(3)]

    real_stat = Path.stat

    def flaky_stat(self, *a, **k):
        if self.name == "f0.jpg":  # vanished mid-walk
            raise FileNotFoundError("raced away")
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    res = derived.evict_local_cache(cache_root, max_gb=1.5 / 1024)  # no crash
    # f0 was never collected (stat raised); f1/f2 (2 MiB) evict down toward 1.5 MiB.
    assert files[0].exists()  # the "vanished"-at-stat file was simply skipped
    assert res.deleted >= 1


def test_evict_local_cache_under_cap_is_noop(tmp_path):
    cache_root = tmp_path / "cache"
    thumbs = cache_root / CACHE / "thumbs"
    thumbs.mkdir(parents=True)
    f = _make_cache_file(thumbs, "f.jpg", 1 << 20, atime=1000)
    res = derived.evict_local_cache(cache_root, max_gb=10.0)
    assert f.exists() and res.deleted == 0 and res.bytes_after == res.bytes_before


# --- Proxy-tier cache eviction (m-implement-proxy-prewarm-cap) -------------- #


def test_evict_proxy_cache_drops_oldest_and_spares_stitch(tmp_path):
    # The proxy cap evicts least-recently-accessed PROXIES down to the cap, and never
    # touches the stitch heroes (costly to regenerate) sharing the same tier root.
    cache_root = tmp_path / "proxytier"
    proxies = cache_root / CACHE / "proxies"
    stitch = cache_root / CACHE / "stitch"
    proxies.mkdir(parents=True)
    stitch.mkdir(parents=True)
    mib = 1 << 20
    pfiles = [_make_cache_file(proxies, f"p{i}.mp4", mib, atime=1000 + i) for i in range(5)]
    hero = _make_cache_file(stitch, "hero.jpg", 3 * mib, atime=1)  # oldest of all

    res = derived.evict_proxy_cache(cache_root, max_gb=3 / 1024)  # 3 MiB cap

    assert not pfiles[0].exists() and not pfiles[1].exists()  # 2 oldest proxies dropped
    assert all(f.exists() for f in pfiles[2:])  # newest 3 proxies kept
    assert hero.exists()  # stitch hero spared despite the oldest atime
    assert res.deleted == 2
    assert res.bytes_after <= 3 * mib


def test_evict_proxy_cache_under_cap_is_noop(tmp_path):
    cache_root = tmp_path / "proxytier"
    proxies = cache_root / CACHE / "proxies"
    proxies.mkdir(parents=True)
    f = _make_cache_file(proxies, "p.mp4", 1 << 20, atime=1000)
    res = derived.evict_proxy_cache(cache_root, max_gb=10.0)
    assert f.exists() and res.deleted == 0 and res.bytes_after == res.bytes_before


def test_atomic_write_failure_publishes_nothing(tmp_path):
    # A failed generation must never leave a half-written cache file at `out`,
    # nor a leftover temp in the cache dir (concurrent-request corruption guard).
    out = tmp_path / CACHE / "thumbs" / "x.jpg"

    def boom(dest):
        dest.write_bytes(b"partial-bytes")  # wrote to the temp...
        raise RuntimeError("generation failed")

    with pytest.raises(RuntimeError):
        derived._atomic_write(out, boom)
    assert not out.exists()  # ...but `out` was never published
    assert list(out.parent.iterdir()) == []  # temp cleaned up


# --- Size-aware freshness (m-fix-stale-derived-cache-thumbnails) ------------ #


def test_generate_writes_size_sidecar(tmp_path):
    # _generate records the source's byte size in a <cachefile>.src sidecar so a later
    # content swap (different size, possibly older mtime) is detected as stale.
    src = FIXTURES / "dji_photo.jpg"
    out = derived.thumbnail(tmp_path / "cache", "dji_photo.jpg", src)
    sidecar = derived._src_sidecar(out)
    assert sidecar.exists()
    assert sidecar.read_text() == str(src.stat().st_size)


def test_is_fresh_stale_on_size_mismatch(tmp_path):
    # The headline bug: a dest path whose content was REPLACED by a file with an OLDER
    # mtime than the cached asset must NOT be served from cache. The size sidecar catches
    # it even though cache_mtime >= source_mtime still holds.
    cache = tmp_path / "cache"
    src = tmp_path / "photo.jpg"
    Image.new("RGB", (40, 30), "red").save(src)
    out = derived.thumbnail(cache, "photo.jpg", src)
    pinned = out.stat().st_mtime  # cache mtime is pinned to the (original) source mtime

    # Replace the source with different-size bytes, keeping mtime <= cache mtime so the
    # legacy mtime-only check would (wrongly) still consider the cache fresh.
    src.write_bytes(b"\0" * (out.stat().st_size + src.stat().st_size + 123))
    os.utime(src, (pinned, pinned))

    assert not derived._is_fresh(out, src)  # size sidecar mismatch -> stale


def test_is_fresh_lenient_without_sidecar(tmp_path):
    # An EXISTING cache file with no .src sidecar (e.g. a proxy generated before this
    # fix) keeps the old mtime-only leniency, so it is NOT force-regenerated.
    src = tmp_path / "s.bin"
    src.write_bytes(b"x" * 10)
    cache = tmp_path / "c.jpg"
    cache.write_bytes(b"y" * 5)  # deliberately a different size than the source
    os.utime(src, (1000.0, 1000.0))
    os.utime(cache, (1000.0, 1000.0))  # cache mtime >= source mtime
    assert not derived._src_sidecar(cache).exists()
    assert derived._is_fresh(cache, src)  # no sidecar -> lenient, still fresh


# --- invalidate() + eviction sidecar handling ------------------------------ #


def test_invalidate_removes_asset_and_sidecar(tmp_path):
    cache_dir = tmp_path / "cache"
    proxy_dir = tmp_path / "proxytier"
    rel = "Place, Region, Country/2024-01-01/clip.mp4"

    # Seed a local-tier poster (cache_dir) and a proxy-tier proxy (proxy_dir), each with
    # its size sidecar, directly at the computed cache paths (no ffmpeg needed).
    poster = derived._cache_path(cache_dir, rel, "posters", ".jpg")
    proxy = derived._cache_path(proxy_dir, rel, "proxies", ".mp4")
    for f in (poster, proxy):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"data")
        derived._src_sidecar(f).write_text("4")

    derived.invalidate(cache_dir, proxy_dir, rel)

    assert not poster.exists() and not derived._src_sidecar(poster).exists()
    assert not proxy.exists() and not derived._src_sidecar(proxy).exists()
    derived.invalidate(cache_dir, proxy_dir, rel)  # idempotent: a second call never raises


def test_evict_cache_ignores_src_sidecars(tmp_path):
    cache_root = tmp_path / "cache"
    thumbs = cache_root / CACHE / "thumbs"
    thumbs.mkdir(parents=True)
    mib = 1 << 20
    big = _make_cache_file(thumbs, "f.jpg", 2 * mib, atime=1000)
    sidecar = thumbs / "f.jpg.src"
    sidecar.write_text("123")
    os.utime(sidecar, (1, 1))  # oldest atime — would be evicted first if counted

    res = derived.evict_local_cache(cache_root, max_gb=1 / 1024)  # 1 MiB cap

    assert not big.exists()  # the real asset evicted
    assert sidecar.exists()  # the sidecar is never swept
    assert res.bytes_before == 2 * mib  # sidecar bytes not counted toward the cap
    assert res.deleted == 1


# --- clear_local_cache remediation ----------------------------------------- #


def test_clear_local_cache_spares_proxies_stitch(tmp_path):
    cache_dir = tmp_path / "cache"
    seeded = {}
    for kind, ext in [("thumbs", ".jpg"), ("previews", ".jpg"), ("posters", ".jpg"),
                      ("collage", ".jpg"), ("proxies", ".mp4"), ("stitch", ".jpg")]:
        d = cache_dir / CACHE / kind
        d.mkdir(parents=True)
        f = d / ("a" + ext)
        f.write_bytes(b"x")
        seeded[kind] = f

    n = derived.clear_local_cache(cache_dir)

    for kind in ("thumbs", "previews", "posters", "collage"):
        assert not seeded[kind].exists()  # cheap local kinds wiped
    assert seeded["proxies"].exists()  # expensive proxy spared
    assert seeded["stitch"].exists()  # expensive Hugin hero spared
    assert n == 4
