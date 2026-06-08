"""Tests for the crash-safe move engine (copy → verify → delete, recoverable)."""

import errno

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


def test_copy_and_verify_uses_provided_source_hash(tmp_path, monkeypatch):
    # When the caller threads in an already-computed source hash, copy_and_verify
    # must NOT re-read the source to hash it — the only sha256_file call is the
    # destination .partial read-back (the integrity check is retained).
    conn = _index(tmp_path)
    src = _src(tmp_path)
    dest = str(tmp_path / "library" / "out.JPG")
    digest = move_engine.sha256_file(src)  # computed BEFORE patching (real impl)
    calls = {"n": 0}
    real = move_engine.sha256_file

    def _counting(path, **kwargs):
        calls["n"] += 1
        return real(path, **kwargs)

    monkeypatch.setattr(move_engine, "sha256_file", _counting)
    try:
        outcome = move_engine.copy_and_verify(conn, BATCH, src, dest, source_sha256=digest)
        row = _moves_row(conn, src)
    finally:
        conn.close()
    assert calls["n"] == 1  # ONLY the dest read-back; source is never re-hashed
    assert outcome.status == "copy_verified"
    assert (tmp_path / "library" / "out.JPG").read_bytes() == b"hello-capture"
    assert row[0] == "copy_verified"
    assert row[1] == digest  # stored source_sha256 == the provided digest
    assert row[1] == row[2]  # source == dest hash


def test_copy_and_verify_provided_hash_mismatch_aborts(tmp_path):
    # A wrong provided hash must still be caught by the dest read-back and abort —
    # the verification guarantee is not weakened by trusting the caller's digest.
    conn = _index(tmp_path)
    src = _src(tmp_path)
    dest = str(tmp_path / "library" / "out.JPG")
    try:
        outcome = move_engine.copy_and_verify(conn, BATCH, src, dest, source_sha256="00" * 32)
        row = _moves_row(conn, src)
    finally:
        conn.close()
    assert outcome.status == "failed"
    assert src.exists()
    assert not (tmp_path / "library" / "out.JPG").exists()
    assert not (tmp_path / "library" / "out.JPG.partial").exists()
    assert row[0] == "failed"


def test_copy_and_verify_provided_hash_resume_skips_recopy(tmp_path, monkeypatch):
    # The provided-hash path keeps idempotent resume: a second call after a
    # copy_verified row short-circuits without re-copying.
    conn = _index(tmp_path)
    src = _src(tmp_path)
    dest = str(tmp_path / "library" / "out.JPG")
    digest = move_engine.sha256_file(src)
    try:
        first = move_engine.copy_and_verify(conn, BATCH, src, dest, source_sha256=digest)
        assert first.status == "copy_verified"
        calls = {"n": 0}
        real_copy = move_engine._copy_file

        def _counting_copy(s, d, **k):
            calls["n"] += 1
            return real_copy(s, d, **k)

        monkeypatch.setattr(move_engine, "_copy_file", _counting_copy)
        second = move_engine.copy_and_verify(conn, BATCH, src, dest, source_sha256=digest)
        assert second.status == "copy_verified"
        assert calls["n"] == 0  # resumed from the copy_verified row, no re-copy
    finally:
        conn.close()


def test_copy_retries_transient_oserror(tmp_path, monkeypatch):
    # A transient OSError (e.g. an SMB blip) on the copy is retried; the next attempt
    # succeeds and the move completes as copy_verified.
    conn = _index(tmp_path)
    src = _src(tmp_path)
    dest = str(tmp_path / "library" / "out.JPG")
    monkeypatch.setattr(move_engine.time, "sleep", lambda _s: None)  # no real backoff wait
    real = move_engine._copy_file
    calls = {"n": 0}

    def _flaky(s, d, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.ECONNRESET, "connection reset by peer")
        return real(s, d, **k)

    monkeypatch.setattr(move_engine, "_copy_file", _flaky)
    try:
        outcome = move_engine.copy_and_verify(
            conn, BATCH, src, dest, retry_attempts=3, retry_backoff_s=0
        )
        row = _moves_row(conn, src)
    finally:
        conn.close()
    assert calls["n"] == 2  # failed once, succeeded on the retry
    assert outcome.status == "copy_verified"
    assert (tmp_path / "library" / "out.JPG").read_bytes() == b"hello-capture"
    assert row[0] == "copy_verified"


def test_enospc_not_retried(tmp_path, monkeypatch):
    # ENOSPC is NOT transient — retrying cannot help, so it fails immediately even
    # with retry_attempts > 1 (the free-space recheck owns disk-full, not the retry).
    conn = _index(tmp_path)
    src = _src(tmp_path)
    dest = str(tmp_path / "library" / "out.JPG")
    monkeypatch.setattr(move_engine.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def _full(s, d, *a, **k):
        calls["n"] += 1
        with open(d, "wb") as fh:
            fh.write(b"partial")
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(move_engine, "_copy_file", _full)
    try:
        outcome = move_engine.copy_and_verify(
            conn, BATCH, src, dest, retry_attempts=3, retry_backoff_s=0
        )
    finally:
        conn.close()
    assert calls["n"] == 1  # not retried
    assert outcome.status == "failed"
    assert not (tmp_path / "library" / "out.JPG.partial").exists()  # partial cleaned


def test_retry_exhausts_then_fails(tmp_path, monkeypatch):
    # Every attempt blips → after retry_attempts the move is marked failed (the
    # group-atomic abort contract is preserved) and the partial is cleaned.
    conn = _index(tmp_path)
    src = _src(tmp_path)
    dest = str(tmp_path / "library" / "out.JPG")
    monkeypatch.setattr(move_engine.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def _always_blip(s, d, *a, **k):
        calls["n"] += 1
        raise OSError(errno.ECONNRESET, "connection reset by peer")

    monkeypatch.setattr(move_engine, "_copy_file", _always_blip)
    try:
        outcome = move_engine.copy_and_verify(
            conn, BATCH, src, dest, retry_attempts=3, retry_backoff_s=0
        )
        row = _moves_row(conn, src)
    finally:
        conn.close()
    assert calls["n"] == 3  # all three attempts used
    assert outcome.status == "failed"
    assert src.exists()
    assert not (tmp_path / "library" / "out.JPG.partial").exists()
    assert row[0] == "failed"


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
