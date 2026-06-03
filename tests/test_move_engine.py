"""Tests for the crash-safe move engine (copy → verify → delete, recoverable)."""

from geosorter import db, move_engine

BATCH = "20260531T120000-abcdef"


def _index(tmp_path):
    conn = db.connect(tmp_path / "index.db", integrity_check=False)
    db.init_index_schema(conn)
    return conn


def _src(tmp_path, name="DJI_0001.JPG", data=b"hello-capture"):
    p = tmp_path / "inbox" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _moves_row(conn, source_path):
    return conn.execute(
        "SELECT status, source_sha256, dest_sha256 FROM moves WHERE source_path=?",
        (str(source_path),),
    ).fetchone()


def test_copy_and_verify_happy(tmp_path):
    conn = _index(tmp_path)
    src = _src(tmp_path)
    dest = str(tmp_path / "library" / "Place" / "2024-07-04" / "out.JPG")
    try:
        outcome = move_engine.copy_and_verify(conn, BATCH, src, dest)
    finally:
        conn.close()
    assert outcome.status == "copy_verified"
    assert (tmp_path / "library" / "Place" / "2024-07-04" / "out.JPG").read_bytes() == b"hello-capture"
    assert src.exists()  # source NOT deleted by copy_and_verify
    conn = db.connect(tmp_path / "index.db", integrity_check=False)
    try:
        row = _moves_row(conn, src)
    finally:
        conn.close()
    assert row[0] == "copy_verified"
    assert row[1] == row[2]  # source_sha256 == dest_sha256


def test_commit_delete_removes_source(tmp_path):
    conn = _index(tmp_path)
    src = _src(tmp_path)
    dest = str(tmp_path / "library" / "out.JPG")
    try:
        outcome = move_engine.copy_and_verify(conn, BATCH, src, dest)
        move_engine.commit_delete(conn, src, outcome.source_sha256)
        row = _moves_row(conn, src)
    finally:
        conn.close()
    assert not src.exists()
    assert row[0] == "source_deleted"


def test_verify_mismatch_aborts(tmp_path, monkeypatch):
    conn = _index(tmp_path)
    src = _src(tmp_path)
    dest = str(tmp_path / "library" / "out.JPG")

    def _corrupt(s, d, *a, **k):
        with open(d, "wb") as fh:
            fh.write(b"CORRUPTED")

    monkeypatch.setattr(move_engine, "_copy_file", _corrupt)
    try:
        outcome = move_engine.copy_and_verify(conn, BATCH, src, dest)
        row = _moves_row(conn, src)
    finally:
        conn.close()
    assert outcome.status == "failed"
    assert src.exists()  # source untouched
    assert not (tmp_path / "library" / "out.JPG").exists()  # no final dest
    assert not (tmp_path / "library" / "out.JPG.partial").exists()  # partial cleaned
    assert row[0] == "failed"


def test_disk_full_cleans_partial(tmp_path, monkeypatch):
    conn = _index(tmp_path)
    src = _src(tmp_path)
    dest = str(tmp_path / "library" / "out.JPG")

    def _boom(s, d, *a, **k):
        with open(d, "wb") as fh:
            fh.write(b"partial")
        raise OSError("No space left on device")

    monkeypatch.setattr(move_engine, "_copy_file", _boom)
    try:
        outcome = move_engine.copy_and_verify(conn, BATCH, src, dest)
    finally:
        conn.close()
    assert outcome.status == "failed"
    assert src.exists()
    assert not (tmp_path / "library" / "out.JPG.partial").exists()


def test_midflight_kill_recovers(tmp_path, monkeypatch):
    # Simulate a kill AFTER copy+verify but BEFORE commit_delete: the moves row
    # is left at copy_verified with source present. Re-running must NOT re-copy
    # (source bytes already verified) and must finish to source_deleted.
    conn = _index(tmp_path)
    src = _src(tmp_path)
    dest = str(tmp_path / "library" / "out.JPG")

    calls = {"n": 0}
    real_copy = move_engine._copy_file

    def _counting(s, d, *a, **k):
        calls["n"] += 1
        return real_copy(s, d)

    monkeypatch.setattr(move_engine, "_copy_file", _counting)
    try:
        first = move_engine.copy_and_verify(conn, BATCH, src, dest)
        assert first.status == "copy_verified"
        assert calls["n"] == 1
        # --- "crash" here: commit_delete never ran ---
        second = move_engine.copy_and_verify(conn, BATCH, src, dest)
        assert second.status == "copy_verified"
        assert calls["n"] == 1  # NOT re-copied
        move_engine.commit_delete(conn, src, second.source_sha256)
        row = _moves_row(conn, src)
    finally:
        conn.close()
    assert not src.exists()
    assert (tmp_path / "library" / "out.JPG").read_bytes() == b"hello-capture"
    assert row[0] == "source_deleted"


def test_sha256_file_reports_progress(tmp_path):
    # on_bytes is called with the cumulative byte count and the digest is unchanged.
    src = _src(tmp_path, data=b"x" * (3 * (1 << 20) + 7))  # 3 chunks + tail
    seen: list[int] = []
    digest = move_engine.sha256_file(src, on_bytes=seen.append)
    assert digest == move_engine.sha256_file(src)  # same digest with/without callback
    assert seen  # at least one tick
    assert seen == sorted(seen)  # monotonic cumulative
    assert seen[-1] == src.stat().st_size  # final tick == file size


def test_copy_and_verify_reports_phases(tmp_path):
    conn = _index(tmp_path)
    src = _src(tmp_path, data=b"capture-bytes" * 100_000)
    dest = str(tmp_path / "library" / "out.JPG")
    calls: list[tuple[str, int, int]] = []
    try:
        outcome = move_engine.copy_and_verify(
            conn, BATCH, src, dest, progress=lambda phase, done, total: calls.append((phase, done, total))
        )
    finally:
        conn.close()
    assert outcome.status == "copy_verified"
    assert (tmp_path / "library" / "out.JPG").read_bytes() == src.read_bytes()  # byte-identical
    phases = {phase for phase, _d, _t in calls}
    assert {"copying", "verifying"} <= phases
    size = src.stat().st_size
    assert all(total == size for _p, _d, total in calls)  # total == source size for every tick
    assert all(done <= total for _p, done, total in calls)


def test_is_already_moved(tmp_path):
    conn = _index(tmp_path)
    src = _src(tmp_path)
    dest = str(tmp_path / "library" / "out.JPG")
    try:
        assert move_engine.is_already_moved(conn, src) is False
        outcome = move_engine.copy_and_verify(conn, BATCH, src, dest)
        move_engine.commit_delete(conn, src, outcome.source_sha256)
        assert move_engine.is_already_moved(conn, src) is True
    finally:
        conn.close()


def test_pending_recovery_recopies(tmp_path):
    # A 'pending' row (crashed mid-copy, partial untrusted) recovers by re-copying
    # since the source is still present.
    conn = _index(tmp_path)
    src = _src(tmp_path)
    dest = str(tmp_path / "library" / "out.JPG")
    sha = move_engine.sha256_file(src)
    # Manually seed a stale pending row + a stale partial, as a crash would leave.
    conn.execute(
        "INSERT INTO moves(batch_id, source_path, dest_path, source_sha256, status) "
        "VALUES (?,?,?,?, 'pending')",
        (BATCH, str(src), dest, sha),
    )
    conn.commit()
    (tmp_path / "library").mkdir(parents=True, exist_ok=True)
    (tmp_path / "library" / "out.JPG.partial").write_bytes(b"garbage")
    try:
        outcome = move_engine.copy_and_verify(conn, BATCH, src, dest)
        row = _moves_row(conn, src)
    finally:
        conn.close()
    assert outcome.status == "copy_verified"
    assert (tmp_path / "library" / "out.JPG").read_bytes() == b"hello-capture"
    assert row[0] == "copy_verified"
