"""Tests for the retroactive panorama re-stitch reconciler (restitch.py).

``run_restitch`` re-runs the Hugin pipeline (``derived.panorama_stitch(force=True)``)
for already-stitched panoramas baked at the old hard-coded equirectangular projection
and records the freshly auto-detected ``stitch_projection``. Hugin is monkeypatched
out (``find_hugin`` + ``panorama_stitch``) so the suite stays fast and deterministic.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from geosorter import db, derived, restitch
from geosorter.derived import StitchResult


def _cfg(tmp_path: Path, idx: Path) -> SimpleNamespace:
    return SimpleNamespace(
        index_db_path=idx,
        library_root=tmp_path / "lib",
        hugin_bin_dir=None,
        proxy_cache_dir=None,  # -> library_root via resolve_proxy_cache_dir
        stitch_canvas="4000x2000",
        stitch_celeste=True,
        stitch_optimise_lens=True,
    )


def _seed(conn, *, status="organized", capture_kind="panorama",
          stitch_status="ok", stitch_projection=None, dest="lib/pano/PANO_0001.JPG"):
    """Insert one files row; return its id."""
    cur = conn.execute(
        "INSERT INTO files (dest_path, filename, media_type, sha256, status, "
        "capture_kind, frame_count, stitch_status, stitch_projection) "
        "VALUES (?, 'PANO_0001.JPG', 'photo', 'h', ?, ?, 3, ?, ?)",
        (dest, status, capture_kind, stitch_status, stitch_projection),
    )
    return cur.lastrowid


def _read(idx, file_id):
    conn = db.connect(idx, integrity_check=False)
    try:
        return conn.execute(
            "SELECT stitch_status, stitch_projection FROM files WHERE id=?", (file_id,)
        ).fetchone()
    finally:
        conn.close()


def test_selects_only_null_projection_by_default(tmp_path):
    idx = tmp_path / "index.db"
    conn = db.connect(idx)
    db.init_index_schema(conn)
    _seed(conn, stitch_status="ok", stitch_projection=None, dest="a")          # target
    _seed(conn, stitch_status="ok", stitch_projection="equirectangular", dest="b")  # already detected
    _seed(conn, stitch_status="failed", stitch_projection=None, dest="c")      # not 'ok'
    _seed(conn, capture_kind=None, stitch_status="ok", stitch_projection=None, dest="d")  # not panorama
    conn.commit()
    conn.close()

    report = restitch.run_restitch(_cfg(tmp_path, idx), dry_run=True)
    assert report.targets == 1


def test_force_all_selects_all_ok_panoramas(tmp_path):
    idx = tmp_path / "index.db"
    conn = db.connect(idx)
    db.init_index_schema(conn)
    _seed(conn, stitch_status="ok", stitch_projection=None, dest="a")
    _seed(conn, stitch_status="ok", stitch_projection="equirectangular", dest="b")
    _seed(conn, stitch_status="failed", stitch_projection=None, dest="c")  # excluded (no hero)
    conn.commit()
    conn.close()

    report = restitch.run_restitch(_cfg(tmp_path, idx), force_all=True, dry_run=True)
    assert report.targets == 2


def test_restitch_records_new_projection(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    (lib / "pano").mkdir(parents=True)
    primary = lib / "pano" / "PANO_0001.JPG"
    primary.write_bytes(b"x")
    idx = tmp_path / "index.db"
    conn = db.connect(idx)
    db.init_index_schema(conn)
    fid = _seed(conn, stitch_status="ok", stitch_projection=None, dest=str(primary))
    conn.execute(
        "INSERT INTO file_companions (primary_file_id, dest_path, companion_type) "
        "VALUES (?, ?, 'panorama_frame')",
        (fid, str(lib / "pano" / "PANO_0002.JPG")),
    )
    conn.commit()
    conn.close()

    seen = {}

    def fake_stitch(cache_root, rel_key, prim, frames, *, force=False, **_):
        seen["force"] = force
        seen["frames"] = list(frames)
        return StitchResult(Path("hero.jpg"), "flat")

    monkeypatch.setattr(derived, "find_hugin", lambda hugin_bin_dir=None: {"x": "x"})
    monkeypatch.setattr(derived, "panorama_stitch", fake_stitch)

    report = restitch.run_restitch(_cfg(tmp_path, idx))
    assert report.restitched == 1
    assert report.projections == {"flat": 1}
    assert seen["force"] is True
    assert len(seen["frames"]) == 1  # the panorama_frame companion was loaded
    assert _read(idx, fid) == ("ok", "flat")


def test_failed_restitch_preserves_row(tmp_path, monkeypatch):
    idx = tmp_path / "index.db"
    conn = db.connect(idx)
    db.init_index_schema(conn)
    fid = _seed(conn, stitch_status="ok", stitch_projection="equirectangular", dest="a")
    conn.commit()
    conn.close()

    def boom(*a, **k):
        raise derived.StitchFailed("degenerate stitch")

    monkeypatch.setattr(derived, "find_hugin", lambda hugin_bin_dir=None: {"x": "x"})
    monkeypatch.setattr(derived, "panorama_stitch", boom)

    report = restitch.run_restitch(_cfg(tmp_path, idx), force_all=True)
    assert report.failed == 1
    assert report.restitched == 0
    assert report.errors
    assert _read(idx, fid) == ("ok", "equirectangular")  # untouched


def test_unavailable_when_hugin_absent(tmp_path, monkeypatch):
    idx = tmp_path / "index.db"
    conn = db.connect(idx)
    db.init_index_schema(conn)
    fid = _seed(conn, stitch_status="ok", stitch_projection=None, dest="a")
    conn.commit()
    conn.close()

    called = {"stitch": False}

    def fake_stitch(*a, **k):
        called["stitch"] = True
        return StitchResult(Path("hero.jpg"), "flat")

    monkeypatch.setattr(derived, "find_hugin", lambda hugin_bin_dir=None: None)
    monkeypatch.setattr(derived, "panorama_stitch", fake_stitch)

    report = restitch.run_restitch(_cfg(tmp_path, idx))
    assert report.unavailable is True
    assert report.restitched == 0
    assert called["stitch"] is False
    assert _read(idx, fid) == ("ok", None)  # DB unchanged


def test_missing_file_is_reported_not_fatal(tmp_path, monkeypatch):
    # A stale/partial row whose primary or a frame left the library makes
    # panorama_stitch raise OSError (FileNotFoundError) from its tile stat() before
    # any Hugin step. That must be reported per-row and skipped, NOT abort the batch
    # (mirrors rescan.py's per-row resilience over the same library).
    idx = tmp_path / "index.db"
    conn = db.connect(idx)
    db.init_index_schema(conn)
    _seed(conn, stitch_status="ok", stitch_projection=None, dest="gone")  # id 1, missing
    f2 = _seed(conn, stitch_status="ok", stitch_projection=None, dest="ok")  # id 2, fine
    conn.commit()
    conn.close()

    calls = []

    def fake_stitch(cache_root, rel_key, primary, frames, *, force=False, **_):
        calls.append(primary)
        if primary == "gone":
            raise FileNotFoundError(primary)
        return StitchResult(Path("hero.jpg"), "flat")

    monkeypatch.setattr(derived, "find_hugin", lambda hugin_bin_dir=None: {"x": "x"})
    monkeypatch.setattr(derived, "panorama_stitch", fake_stitch)

    report = restitch.run_restitch(_cfg(tmp_path, idx))
    assert report.failed == 1
    assert report.restitched == 1  # the second panorama was still processed
    assert report.errors
    assert calls == ["gone", "ok"]  # did not abort after the first
    assert _read(idx, f2) == ("ok", "flat")


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    idx = tmp_path / "index.db"
    conn = db.connect(idx)
    db.init_index_schema(conn)
    fid = _seed(conn, stitch_status="ok", stitch_projection=None, dest="a")
    conn.commit()
    conn.close()

    called = {"hugin": False, "stitch": False}
    monkeypatch.setattr(
        derived, "find_hugin",
        lambda hugin_bin_dir=None: called.__setitem__("hugin", True) or {"x": "x"},
    )
    monkeypatch.setattr(
        derived, "panorama_stitch",
        lambda *a, **k: called.__setitem__("stitch", True) or StitchResult(Path("h"), "flat"),
    )

    report = restitch.run_restitch(_cfg(tmp_path, idx), dry_run=True)
    assert report.targets == 1
    assert called == {"hugin": False, "stitch": False}  # no probe, no stitch
    assert _read(idx, fid) == ("ok", None)  # DB unchanged
