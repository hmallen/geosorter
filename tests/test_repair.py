"""Tests for the broken-capture repair flow (m-repair-broken-captures)."""

from __future__ import annotations

from pathlib import Path

import pytest

from geosorter import config as config_mod
from geosorter import db, repair
from geosorter.config import Config
from geosorter.repair import (
    ProbeResult,
    UntruncNotFound,
    accept_repair,
    delete_broken,
    discard_repair,
    find_untrunc,
    install_untrunc,
    parse_dji_name,
    reference_candidates,
    run_repair,
    scan_broken,
    select_untrunc_asset,
)


def _cfg(tmp_path) -> Config:
    return Config(
        inbox_path=tmp_path / "inbox",
        library_root=tmp_path / "library",
        index_db_path=tmp_path / "index.db",
        geonames_db_path=tmp_path / "geonames.db",
        spatial_index="rtree",
        cache_dir=tmp_path / "cache",
    )


def _seed(conn, *, dest_path, filename, media_type="video", status="quarantined",
          local_date=None, place_string=None, codec=None, width=None, height=None,
          duration_s=None, sha256="deadbeef", batch_id=None):
    cur = conn.execute(
        "INSERT INTO files(dest_path, filename, media_type, status, local_date, "
        "place_string, codec, width, height, duration_s, sha256, batch_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (dest_path, filename, media_type, status, local_date, place_string,
         codec, width, height, duration_s, sha256, batch_id),
    )
    return cur.lastrowid


@pytest.fixture
def cfg_and_conn(tmp_path):
    cfg = _cfg(tmp_path)
    Path(cfg.library_root).mkdir()
    conn = db.connect(cfg.index_db_path, integrity_check=False)
    db.init_index_schema(conn)
    yield cfg, conn
    conn.close()


OK_PROBE = ProbeResult(ok=True, codec="h264", width=3840, height=2160,
                       duration_s=61.5)
# Real ffprobe stderr shape: the moov line comes BEFORE the generic summary line
# (classification must scan the whole message, not just the last line).
MOOV_PROBE = ProbeResult(
    ok=False,
    error="[mov,mp4 @ 0x1] moov atom not found\n"
          "x.MP4: Invalid data found when processing input",
)


# --------------------------------------------------------------------------- #
# Name parsing + reference ranking
# --------------------------------------------------------------------------- #


def test_parse_dji_name_classic():
    parsed = parse_dji_name("DJI_0076.MP4")
    assert (parsed.series, parsed.seq, parsed.segment) == ("classic", 76, None)


def test_parse_dji_name_split_segment():
    parsed = parse_dji_name("DJI_0119_2.MP4")
    assert (parsed.series, parsed.seq, parsed.segment) == ("classic", 119, 2)


def test_parse_dji_name_timestamped():
    parsed = parse_dji_name("DJI_20240804182951_0017_D.MP4")
    assert (parsed.series, parsed.ts, parsed.seq) == (
        "timestamped", "20240804182951", 17,
    )


def test_parse_dji_name_other():
    assert parse_dji_name("IMG_1234.MOV").series == "other"


def test_reference_candidates_prefers_split_sibling(cfg_and_conn):
    cfg, conn = cfg_and_conn
    lib = str(cfg.library_root)
    target = _seed(conn, dest_path=f"{lib}\\_no-gps\\2023-07-07\\DJI_0119_2.MP4",
                   filename="DJI_0119_2.MP4")
    sibling = _seed(conn, dest_path=f"{lib}\\A\\2023-07-07\\DJI_0119_1.MP4",
                    filename="DJI_0119_1.MP4", status="organized",
                    local_date="2023-07-07", codec="h265", width=3840, height=2160)
    near = _seed(conn, dest_path=f"{lib}\\A\\2023-07-07\\DJI_0121.MP4",
                 filename="DJI_0121.MP4", status="organized",
                 local_date="2023-07-07", codec="h265")
    _seed(conn, dest_path=f"{lib}\\B\\2026-01-01\\DJI_20260101000000_0001_D.MP4",
          filename="DJI_20260101000000_0001_D.MP4", status="organized",
          local_date="2026-01-01", codec="h264")
    # Broken (codec NULL) rows are never offered as references.
    _seed(conn, dest_path=f"{lib}\\_no-gps\\2023-07-07\\DJI_0122_2.MP4",
          filename="DJI_0122_2.MP4")
    conn.commit()

    candidates = reference_candidates(cfg, target)
    assert [c.file_id for c in candidates[:2]] == [sibling, near]
    assert candidates[0].recommended is True
    assert any("split recording" in r for r in candidates[0].reasons)
    assert all(c.codec is not None for c in candidates)
    assert candidates[0].rel_path == "A/2023-07-07/DJI_0119_1.MP4"


def test_reference_candidates_unknown_id(cfg_and_conn):
    cfg, _ = cfg_and_conn
    with pytest.raises(ValueError):
        reference_candidates(cfg, 999)


def test_reference_candidates_no_recommendation_on_tie(cfg_and_conn):
    cfg, conn = cfg_and_conn
    lib = str(cfg.library_root)
    target = _seed(conn, dest_path=f"{lib}\\_no-gps\\x\\IMG_A.MP4",
                   filename="IMG_A.MP4")
    _seed(conn, dest_path=f"{lib}\\A\\IMG_B.MP4", filename="IMG_B.MP4",
          status="organized", codec="h264")
    _seed(conn, dest_path=f"{lib}\\B\\IMG_C.MP4", filename="IMG_C.MP4",
          status="organized", codec="h264")
    conn.commit()
    candidates = reference_candidates(cfg, target)
    # Non-DJI names, no dates: an all-zero tie must not fake a recommendation.
    assert all(not c.recommended for c in candidates)


# --------------------------------------------------------------------------- #
# Scan
# --------------------------------------------------------------------------- #


def test_scan_broken_classifies(cfg_and_conn):
    cfg, conn = cfg_and_conn
    lib = Path(cfg.library_root)
    quarantine = lib / "_no-gps" / "2023-07-05"
    quarantine.mkdir(parents=True)

    healthy = quarantine / "DJI_0001.MP4"
    healthy.write_bytes(b"healthy-bytes")
    broken = quarantine / "DJI_0002.MP4"
    broken.write_bytes(b"truncated-bytes")
    empty = quarantine / "DJI_0003.MP4"
    empty.write_bytes(b"")
    photo = quarantine / "DJI_0004.JPG"
    photo.write_bytes(b"jpegish")

    _seed(conn, dest_path=str(healthy), filename=healthy.name)
    b_id = _seed(conn, dest_path=str(broken), filename=broken.name)
    z_id = _seed(conn, dest_path=str(empty), filename=empty.name)
    m_id = _seed(conn, dest_path=str(quarantine / "DJI_0005.MP4"),
                 filename="DJI_0005.MP4")
    _seed(conn, dest_path=str(photo), filename=photo.name, media_type="photo")
    # Organized rows are out of scope for the quarantine sweep.
    _seed(conn, dest_path=str(lib / "A" / "DJI_0009.MP4"), filename="DJI_0009.MP4",
          status="organized", codec="h264")
    conn.commit()

    def probe_fn(path):
        return MOOV_PROBE if Path(path).name == "DJI_0002.MP4" else OK_PROBE

    seen = []
    report = scan_broken(cfg, progress=seen.append, probe_fn=probe_fn)
    assert report.checked == 5  # quarantined rows only
    assert report.ok == 2  # the healthy video + the non-empty photo
    assert len(seen) == 5
    by_id = {item.file_id: item for item in report.items}
    assert by_id[b_id].status == "no-moov"
    assert "moov atom not found" in by_id[b_id].error
    assert by_id[z_id].status == "zero-byte"
    assert by_id[m_id].status == "missing"
    assert by_id[b_id].rel_path == "_no-gps/2023-07-05/DJI_0002.MP4"
    assert by_id[b_id].date == "2023-07-05"  # folder name fills the NULL local_date


# --------------------------------------------------------------------------- #
# untrunc discovery + repair
# --------------------------------------------------------------------------- #


def test_find_untrunc_in_directory(tmp_path):
    exe = tmp_path / "untrunc.exe"
    exe.write_bytes(b"x")
    assert find_untrunc(tmp_path) == str(exe)
    assert find_untrunc(tmp_path / "missing") is None
    assert find_untrunc(exe) == str(exe)


def _repair_fixture(cfg, conn):
    lib = Path(cfg.library_root)
    quarantine = lib / "_no-gps" / "2022-09-07"
    quarantine.mkdir(parents=True)
    broken = quarantine / "DJI_0771.MP4"
    broken.write_bytes(b"broken-video-data")
    ref = lib / "A" / "DJI_0770.MP4"
    ref.parent.mkdir(parents=True)
    ref.write_bytes(b"reference-video-data")
    target_id = _seed(conn, dest_path=str(broken), filename=broken.name)
    ref_id = _seed(conn, dest_path=str(ref), filename=ref.name,
                   status="organized", codec="h264")
    conn.commit()
    return broken, ref, target_id, ref_id


def test_run_repair_success(cfg_and_conn):
    cfg, conn = cfg_and_conn
    broken, ref, target_id, ref_id = _repair_fixture(cfg, conn)

    def runner(exe, reference, broken_copy, on_poll):
        assert reference == ref
        assert broken_copy.name == f"{target_id}_DJI_0771.MP4"
        assert broken_copy.read_bytes() == b"broken-video-data"
        # anthwlock-style output name, next to the input.
        out = broken_copy.with_name(f"{broken_copy.stem}_fixed.MP4")
        out.write_bytes(b"repaired-video-data")
        on_poll()
        return 0, ["untrunc log line"]

    phases = []
    result = run_repair(
        cfg, target_id, ref_id,
        progress=lambda phase, done, total: phases.append(phase),
        probe_fn=lambda path: OK_PROBE, runner=runner,
    )
    assert result.status == "ok"
    assert result.warning is None  # full-size recovery — nothing suspicious
    assert result.codec == "h264"
    assert result.size == len(b"repaired-video-data")
    assert result.fixed_rel == f"_repair/fixed/{target_id}_DJI_0771.MP4"
    assert Path(result.fixed_path).read_bytes() == b"repaired-video-data"
    # The library original was never touched; the backup is the untrunc input.
    assert broken.read_bytes() == b"broken-video-data"
    assert Path(result.backup_path).read_bytes() == b"broken-video-data"
    assert {"backup", "repair", "verify"} <= set(phases)


def test_run_repair_tiny_output_warns(cfg_and_conn):
    cfg, conn = cfg_and_conn
    broken, _, target_id, ref_id = _repair_fixture(cfg, conn)
    # Make the broken source big enough that a 5-byte "recovery" is clearly a stub.
    broken.write_bytes(b"x" * 4096)

    def runner(exe, reference, broken_copy, on_poll):
        broken_copy.with_name(f"{broken_copy.stem}_fixed.MP4").write_bytes(b"stub!")
        return 0, []

    result = run_repair(cfg, target_id, ref_id,
                        probe_fn=lambda path: OK_PROBE, runner=runner)
    assert result.status == "ok"  # human verification still decides
    assert "recovered only 0%" in result.warning
    # A full-size recovery carries no warning (see test_run_repair_success).


def test_run_repair_no_output_fails(cfg_and_conn):
    cfg, conn = cfg_and_conn
    _, _, target_id, ref_id = _repair_fixture(cfg, conn)
    result = run_repair(
        cfg, target_id, ref_id,
        probe_fn=lambda path: OK_PROBE,
        runner=lambda exe, reference, broken_copy, on_poll: (2, ["boom"]),
    )
    assert result.status == "failed"
    assert "no output" in result.error
    assert result.output_tail == ["boom"]


def test_run_repair_undecodable_reference_fails_early(cfg_and_conn):
    cfg, conn = cfg_and_conn
    _, _, target_id, ref_id = _repair_fixture(cfg, conn)
    result = run_repair(
        cfg, target_id, ref_id,
        probe_fn=lambda path: MOOV_PROBE,
        runner=lambda *a: pytest.fail("untrunc must not run on a bad reference"),
    )
    assert result.status == "failed"
    assert "reference" in result.error


def test_run_repair_undecodable_output_fails(cfg_and_conn):
    cfg, conn = cfg_and_conn
    _, _, target_id, ref_id = _repair_fixture(cfg, conn)

    def runner(exe, reference, broken_copy, on_poll):
        broken_copy.with_name(f"{broken_copy.stem}_fixed.MP4").write_bytes(b"junk")
        return 0, []

    result = run_repair(
        cfg, target_id, ref_id,
        probe_fn=lambda path: OK_PROBE if Path(path).name == "DJI_0770.MP4"
        else MOOV_PROBE,
        runner=runner,
    )
    assert result.status == "failed"
    assert "not decodable" in result.error
    # A verifiably-bad output must never linger where accept could find it.
    assert not (Path(cfg.library_root) / "_repair" / "fixed"
                / f"{target_id}_DJI_0771.MP4").exists()


def test_run_repair_requires_untrunc(cfg_and_conn, monkeypatch):
    cfg, conn = cfg_and_conn
    _, _, target_id, ref_id = _repair_fixture(cfg, conn)
    monkeypatch.setattr(repair.shutil, "which", lambda name: None)
    with pytest.raises(UntruncNotFound):
        run_repair(cfg, target_id, ref_id)


# --------------------------------------------------------------------------- #
# Accept / discard / delete
# --------------------------------------------------------------------------- #


def test_accept_repair_swaps_and_updates_row(cfg_and_conn):
    cfg, conn = cfg_and_conn
    broken, _, target_id, _ = _repair_fixture(cfg, conn)
    fixed_dir = Path(cfg.library_root) / "_repair" / "fixed"
    fixed_dir.mkdir(parents=True)
    (fixed_dir / f"{target_id}_DJI_0771.MP4").write_bytes(b"repaired-video-data")
    # A cached placeholder poster for the broken content must die on accept
    # (cache files swap the media extension for .jpg — see derived._cache_path).
    rel = "_no-gps/2022-09-07/DJI_0771.MP4"
    poster = (Path(cfg.cache_dir) / ".geosorter-cache" / "posters"
              / "_no-gps" / "2022-09-07" / "DJI_0771.jpg")
    poster.parent.mkdir(parents=True)
    poster.write_bytes(b"placeholder")

    out = accept_repair(cfg, target_id, probe_fn=lambda path: OK_PROBE)
    assert out["path"] == rel
    assert broken.read_bytes() == b"repaired-video-data"
    assert not poster.exists()
    row = conn.execute(
        "SELECT sha256, codec, width, height, duration_s FROM files WHERE id=?",
        (target_id,),
    ).fetchone()
    assert row[0] != "deadbeef" and len(row[0]) == 64
    assert tuple(row[1:]) == ("h264", 3840, 2160, 61.5)


def test_accept_repair_without_output(cfg_and_conn):
    cfg, conn = cfg_and_conn
    _, _, target_id, _ = _repair_fixture(cfg, conn)
    with pytest.raises(ValueError, match="awaiting acceptance"):
        accept_repair(cfg, target_id, probe_fn=lambda path: OK_PROBE)


def test_discard_repair_removes_work_files(cfg_and_conn):
    cfg, conn = cfg_and_conn
    _, _, target_id, _ = _repair_fixture(cfg, conn)
    root = Path(cfg.library_root) / "_repair"
    (root / "fixed").mkdir(parents=True)
    (root / "backups").mkdir(parents=True)
    fixed = root / "fixed" / f"{target_id}_DJI_0771.MP4"
    backup = root / "backups" / f"{target_id}_DJI_0771.MP4"
    fixed.write_bytes(b"x")
    backup.write_bytes(b"y")

    out = discard_repair(cfg, target_id)
    assert not fixed.exists() and not backup.exists()
    assert len(out["removed"]) == 2


def test_delete_broken_zero_byte(cfg_and_conn):
    cfg, conn = cfg_and_conn
    lib = Path(cfg.library_root)
    quarantine = lib / "_no-gps" / "2023-07-05"
    quarantine.mkdir(parents=True)
    empty = quarantine / "DJI_0106.MP4"
    empty.write_bytes(b"")
    srt = quarantine / "DJI_0106.SRT"
    srt.write_bytes(b"telemetry")
    file_id = _seed(conn, dest_path=str(empty), filename=empty.name,
                    batch_id="batch1")
    conn.execute(
        "INSERT INTO file_companions(primary_file_id, dest_path, companion_type) "
        "VALUES (?,?,?)",
        (file_id, str(srt), "srt"),
    )
    conn.execute(
        "INSERT INTO moves(file_id, batch_id, source_path, dest_path, "
        "source_sha256, status) VALUES (?,?,?,?,?,'copy_verified')",
        (file_id, "batch1", "inbox/DJI_0106.MP4", str(empty), "deadbeef"),
    )
    conn.commit()

    out = delete_broken(cfg, file_id, probe_fn=lambda path: pytest.fail(
        "a zero-byte file must not be ffprobed"))
    assert sorted(out["deleted"]) == ["DJI_0106.MP4", "DJI_0106.SRT"]
    assert not empty.exists() and not srt.exists()
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM moves").fetchone()[0] == 0


def test_delete_broken_refuses_healthy_file(cfg_and_conn):
    cfg, conn = cfg_and_conn
    lib = Path(cfg.library_root)
    quarantine = lib / "_no-gps" / "2023-07-05"
    quarantine.mkdir(parents=True)
    healthy = quarantine / "DJI_0001.MP4"
    healthy.write_bytes(b"good-bytes")
    file_id = _seed(conn, dest_path=str(healthy), filename=healthy.name)
    conn.commit()

    with pytest.raises(ValueError, match="probes healthy"):
        delete_broken(cfg, file_id, probe_fn=lambda path: OK_PROBE)
    assert healthy.exists()
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1


def test_delete_broken_truncated_video(cfg_and_conn):
    cfg, conn = cfg_and_conn
    lib = Path(cfg.library_root)
    quarantine = lib / "_no-gps" / "2023-07-05"
    quarantine.mkdir(parents=True)
    broken = quarantine / "DJI_0002.MP4"
    broken.write_bytes(b"truncated")
    file_id = _seed(conn, dest_path=str(broken), filename=broken.name)
    conn.commit()

    out = delete_broken(cfg, file_id, probe_fn=lambda path: MOOV_PROBE)
    assert out["deleted"] == ["DJI_0002.MP4"]
    assert not broken.exists()


# --------------------------------------------------------------------------- #
# Installer (`geosorter install-untrunc`)
# --------------------------------------------------------------------------- #


def _fake_zip_bytes(*names: str) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in names:
            z.writestr(name, b"binary!")
    return buf.getvalue()


def test_select_untrunc_asset_prefers_x64():
    assets = [
        {"name": "untrunc_x32.zip", "browser_download_url": "u32"},
        {"name": "untrunc_x64.zip", "browser_download_url": "u64"},
        {"name": "source.tar.gz", "browser_download_url": "src"},
    ]
    assert select_untrunc_asset(assets)["name"] == "untrunc_x64.zip"
    assert select_untrunc_asset(assets[:1])["name"] == "untrunc_x32.zip"
    with pytest.raises(RuntimeError, match="no Windows"):
        select_untrunc_asset([{"name": "source.tar.gz"}])


def test_install_untrunc_downloads_flattens_and_verifies(tmp_path):
    payload = _fake_zip_bytes(
        "untrunc_x64/untrunc.exe", "untrunc_x64/AVCODEC-57.DLL",
        "untrunc_x64/untrunc-gui.exe",
    )
    fetched: dict = {}
    verified: list = []

    def fetch_json(url):
        fetched["api"] = url
        return {
            "tag_name": "latest",
            "assets": [{
                "name": "untrunc_x64.zip", "size": len(payload),
                "browser_download_url": "https://example.invalid/untrunc_x64.zip",
            }],
        }

    def fetch_to_file(url, dest, on_bytes):
        fetched["asset"] = url
        dest.write_bytes(payload)
        if on_bytes:
            on_bytes(len(payload), len(payload))

    progress: list = []
    result = install_untrunc(
        tmp_path / "tools", on_bytes=lambda d, t: progress.append((d, t)),
        fetch_json=fetch_json, fetch_to_file=fetch_to_file,
        verify=verified.append,
    )
    assert fetched["api"] == repair.UNTRUNC_RELEASE_API
    assert result.exe_path == tmp_path / "tools" / "untrunc.exe"  # flattened
    assert result.exe_path.read_bytes() == b"binary!"
    assert (tmp_path / "tools" / "AVCODEC-57.DLL").is_file()  # DLLs beside the exe
    assert not (tmp_path / "tools" / "untrunc_x64.zip").exists()  # archive cleaned
    assert verified == [result.exe_path]
    assert progress and result.release_tag == "latest"


def test_install_untrunc_reuses_existing_binary(tmp_path):
    dest = tmp_path / "tools"
    dest.mkdir()
    (dest / "untrunc.exe").write_bytes(b"already-here")
    verified: list = []

    def no_fetch(*a, **k):
        pytest.fail("an existing install must not trigger a download")

    result = install_untrunc(dest, fetch_json=no_fetch, fetch_to_file=no_fetch,
                             verify=verified.append)
    assert result.reused is True
    assert result.exe_path == dest / "untrunc.exe"
    assert verified == [result.exe_path]  # a stale/broken binary must still fail


def test_install_untrunc_force_redownloads_over_existing(tmp_path):
    dest = tmp_path / "tools"
    dest.mkdir()
    (dest / "untrunc.exe").write_bytes(b"old")
    payload = _fake_zip_bytes("untrunc_x64/untrunc.exe")
    result = install_untrunc(
        dest, force=True,
        fetch_json=lambda url: {"tag_name": "latest", "assets": [{
            "name": "untrunc_x64.zip", "size": len(payload),
            "browser_download_url": "https://example.invalid/z",
        }]},
        fetch_to_file=lambda url, d, on_bytes: d.write_bytes(payload),
        verify=lambda exe: None,
    )
    assert result.reused is False
    assert result.exe_path.read_bytes() == b"binary!"  # replaced, not reused


def test_install_untrunc_without_exe_in_zip_fails(tmp_path):
    payload = _fake_zip_bytes("untrunc_x64/README.txt")
    with pytest.raises(RuntimeError, match="did not contain untrunc.exe"):
        install_untrunc(
            tmp_path / "tools",
            fetch_json=lambda url: {"assets": [{
                "name": "untrunc_x64.zip",
                "browser_download_url": "https://example.invalid/z",
            }]},
            fetch_to_file=lambda url, dest, on_bytes: dest.write_bytes(payload),
            verify=lambda exe: None,
        )


def test_set_untrunc_path_rewrites_config(tmp_path):
    cfg_file = tmp_path / "geosorter.toml"
    cfg_file.write_text(
        "inbox_path = 'Z:\\in'\n# untrunc_path = 'old-comment'\n", encoding="utf-8"
    )
    assert config_mod.set_untrunc_path(cfg_file, r"C:\tools\untrunc.exe") is True
    text = cfg_file.read_text(encoding="utf-8")
    assert "untrunc_path = 'C:\\tools\\untrunc.exe'" in text
    assert "# untrunc_path = 'old-comment'" in text  # comments are never rewritten
    assert config_mod.load(cfg_file).untrunc_path == Path(r"C:\tools\untrunc.exe")
    assert config_mod.set_untrunc_path(tmp_path / "missing.toml", "x") is False


def test_cli_install_untrunc_writes_config(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from geosorter.cli import cli as cli_group

    cfg_file = tmp_path / "geosorter.toml"
    cfg_file.write_text(f"index_db_path = '{tmp_path / 'index.db'}'\n",
                        encoding="utf-8")
    exe = tmp_path / "tools" / "untrunc.exe"

    monkeypatch.setattr(repair, "find_untrunc", lambda p: None)
    monkeypatch.setattr(
        repair, "install_untrunc",
        lambda dest, force=False, on_bytes=None: repair.InstallResult(
            exe_path=exe, asset_name="untrunc_x64.zip", size=1, release_tag="latest",
        ),
    )
    result = CliRunner().invoke(
        cli_group, ["install-untrunc", "--config", str(cfg_file)]
    )
    assert result.exit_code == 0, result.output
    assert "Installed untrunc_x64.zip" in result.output
    assert config_mod.load(cfg_file).untrunc_path == exe


def test_cli_install_untrunc_noop_when_present(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from geosorter.cli import cli as cli_group

    cfg_file = tmp_path / "geosorter.toml"
    cfg_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(repair, "find_untrunc", lambda p: r"C:\already\untrunc.exe")
    monkeypatch.setattr(
        repair, "install_untrunc",
        lambda *a, **k: pytest.fail("must not reinstall without --force"),
    )
    result = CliRunner().invoke(
        cli_group, ["install-untrunc", "--config", str(cfg_file)]
    )
    assert result.exit_code == 0, result.output
    assert "already available" in result.output


# --------------------------------------------------------------------------- #
# HTTP routes
# --------------------------------------------------------------------------- #


def _wait_done(client, url, timeout_s=5.0):
    import time as _time

    deadline = _time.monotonic() + timeout_s
    while _time.monotonic() < deadline:
        state = client.get(url).json()
        if state["state"] in ("done", "error"):
            return state
        _time.sleep(0.02)
    pytest.fail(f"job at {url} never reached a terminal state")


@pytest.fixture
def repair_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from geosorter import api
    from geosorter.jobs import JobManager

    cfg = _cfg(tmp_path)
    lib = Path(cfg.library_root)
    quarantine = lib / "_no-gps" / "2023-07-05"
    quarantine.mkdir(parents=True)
    healthy = quarantine / "DJI_0001.MP4"
    healthy.write_bytes(b"healthy-bytes")
    empty = quarantine / "DJI_0003.MP4"
    empty.write_bytes(b"")

    conn = db.connect(cfg.index_db_path, integrity_check=False)
    db.init_index_schema(conn)
    healthy_id = _seed(conn, dest_path=str(healthy), filename=healthy.name)
    empty_id = _seed(conn, dest_path=str(empty), filename=empty.name)
    conn.commit()
    conn.close()

    # Deterministic fakes: no real ffprobe/untrunc in the HTTP tests.
    monkeypatch.setattr(repair, "probe_file", lambda path: OK_PROBE)
    manager = JobManager(
        cfg,
        repair_scan_fn=lambda c, progress=None: scan_broken(
            c, progress=progress, probe_fn=lambda path: OK_PROBE
        ),
    )
    client = TestClient(api.create_app(cfg, job_manager=manager))
    return client, healthy_id, empty_id


def test_api_repair_scan_flow(repair_client):
    client, _healthy_id, empty_id = repair_client
    job_id = client.post("/api/repair/scan").json()["job_id"]
    state = _wait_done(client, f"/api/repair/scan/status/{job_id}")
    assert state["state"] == "done"
    assert state["checked"] == 2 and state["broken"] == 1
    assert state["items"][0]["id"] == empty_id
    assert state["items"][0]["status"] == "zero-byte"
    assert state["items"][0]["path"] == "_no-gps/2023-07-05/DJI_0003.MP4"
    # Re-POSTing while nothing runs starts a fresh job with its own id.
    assert client.get(f"/api/repair/scan/status/{job_id}").status_code == 200


def test_api_repair_run_without_untrunc_409(repair_client, monkeypatch):
    client, healthy_id, empty_id = repair_client
    monkeypatch.setattr(repair, "find_untrunc", lambda p: None)
    resp = client.post(
        "/api/repair/run",
        json={"file_id": empty_id, "reference_id": healthy_id},
    )
    assert resp.status_code == 409
    assert "untrunc" in resp.json()["detail"]["message"]


def test_api_repair_run_unknown_file_404(repair_client, monkeypatch):
    client, healthy_id, _ = repair_client
    monkeypatch.setattr(repair, "find_untrunc", lambda p: "untrunc.exe")
    resp = client.post(
        "/api/repair/run", json={"file_id": 999, "reference_id": healthy_id}
    )
    assert resp.status_code == 404


def test_api_repair_delete_refuses_healthy(repair_client):
    client, healthy_id, _ = repair_client
    resp = client.post("/api/repair/delete", json={"file_id": healthy_id})
    assert resp.status_code == 409
    assert "healthy" in resp.json()["detail"]["message"]


def test_api_repair_delete_zero_byte(repair_client):
    client, _, empty_id = repair_client
    resp = client.post("/api/repair/delete", json={"file_id": empty_id})
    assert resp.status_code == 200
    assert resp.json()["deleted"] == ["DJI_0003.MP4"]
    # The quarantine list no longer carries the pruned row.
    ids = [f["id"] for f in client.get("/api/quarantine").json()["features"]]
    assert empty_id not in ids


def test_api_repair_accept_without_output_409(repair_client):
    client, healthy_id, _ = repair_client
    resp = client.post("/api/repair/accept", json={"file_id": healthy_id})
    assert resp.status_code == 409
    assert "awaiting acceptance" in resp.json()["detail"]["message"]


def test_api_repair_untrunc_probe(repair_client, monkeypatch):
    client, _, _ = repair_client
    monkeypatch.setattr(repair, "find_untrunc", lambda p: None)
    assert client.get("/api/repair/untrunc").json() == {
        "available": False, "path": None,
    }
