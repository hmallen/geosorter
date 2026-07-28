"""Tests for the pending-duplicate backlog module (record / list / dismiss)."""

import json

from geosorter import db, duplicates


def _conn(tmp_path):
    conn = db.connect(tmp_path / "index.db", integrity_check=False)
    db.init_index_schema(conn)
    return conn


def _record(conn, source, companions=(), *, sha="abc123", matched_file_id=None,
            matched_dest="C:/lib/Place/x.JPG", batch_id="B1"):
    duplicates.record(
        conn,
        source_path=str(source),
        sha256=sha,
        companion_paths=[str(c) for c in companions],
        matched_file_id=matched_file_id,
        matched_dest_path=matched_dest,
        batch_id=batch_id,
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM duplicates WHERE source_path=?", (str(source),)
    ).fetchone()[0]


def _add(inbox, name, data=b"dup-bytes"):
    p = inbox / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _seed_match(conn, *, sha="abc123", dest="C:/lib/Place/x.JPG"):
    """A live library row for ``sha`` — dismiss re-confirms the match against it."""
    conn.execute(
        "INSERT INTO files(dest_path, filename, media_type, sha256, status) "
        "VALUES (?, 'x.JPG', 'photo', ?, 'organized')",
        (dest, sha),
    )
    conn.commit()


def test_record_upserts_on_source_path(tmp_path):
    conn = _conn(tmp_path)
    try:
        rid = _record(conn, tmp_path / "inbox" / "a.JPG", batch_id="B1")
        first_seen = conn.execute(
            "SELECT first_seen_at FROM duplicates WHERE id=?", (rid,)
        ).fetchone()[0]
        rid2 = _record(conn, tmp_path / "inbox" / "a.JPG", sha="def456", batch_id="B2")
        assert rid2 == rid  # one row per source_path
        row = conn.execute(
            "SELECT sha256, batch_id, first_seen_at FROM duplicates WHERE id=?", (rid,)
        ).fetchone()
        assert row[0] == "def456" and row[1] == "B2"  # volatile fields refreshed
        assert row[2] == first_seen  # first-seen timestamp preserved
    finally:
        conn.close()


def test_prune_and_list_rows(tmp_path):
    conn = _conn(tmp_path)
    try:
        _record(conn, tmp_path / "inbox" / "b.JPG")
        _record(conn, tmp_path / "inbox" / "a.JPG")
        rows = duplicates.list_rows(conn)
        assert [r[1] for r in rows] == [  # ordered by id (insertion), not path
            str(tmp_path / "inbox" / "b.JPG"),
            str(tmp_path / "inbox" / "a.JPG"),
        ]
        duplicates.prune(conn, tmp_path / "inbox" / "b.JPG")
        assert len(duplicates.list_rows(conn)) == 1
        duplicates.prune(conn, tmp_path / "inbox" / "never-recorded.JPG")  # no-op
    finally:
        conn.close()


def test_dismiss_moves_group_with_subpath_and_logs(tmp_path):
    inbox = tmp_path / "inbox"
    prim = _add(inbox, "card/DJI_0001.JPG")
    dng = _add(inbox, "card/DJI_0001.DNG", b"raw")
    conn = _conn(tmp_path)
    try:
        _seed_match(conn, dest="C:/lib/P/x.JPG")
        rid = _record(conn, prim, [dng], matched_dest="C:/lib/P/x.JPG", batch_id="B7")
        res = duplicates.dismiss(conn, [rid], inbox)
        assert res.dismissed == 1 and res.skipped == 0 and res.failures == []
        # Whole group moved, inbox subpath preserved.
        assert not prim.exists() and not dng.exists()
        assert (inbox / "_duplicates" / "card" / "DJI_0001.JPG").read_bytes() == b"dup-bytes"
        assert (inbox / "_duplicates" / "card" / "DJI_0001.DNG").read_bytes() == b"raw"
        # Same TSV log line shape as relocate_duplicates=on.
        log = (inbox / "_duplicates" / "duplicates.log").read_text(encoding="utf-8")
        fields = log.strip().split("\t")
        assert fields[1:] == ["B7", str(prim), "C:/lib/P/x.JPG"]
        assert duplicates.list_rows(conn) == []
    finally:
        conn.close()


def test_dismiss_suffixes_on_collision(tmp_path):
    # A file already sitting at the _duplicates/ relpath must not be clobbered.
    inbox = tmp_path / "inbox"
    _add(inbox, "_duplicates/d/DJI_0002.JPG", b"earlier")
    prim = _add(inbox, "d/DJI_0002.JPG", b"later")
    conn = _conn(tmp_path)
    try:
        _seed_match(conn)
        rid = _record(conn, prim)
        res = duplicates.dismiss(conn, [rid], inbox)
        assert res.dismissed == 1
        assert (inbox / "_duplicates" / "d" / "DJI_0002.JPG").read_bytes() == b"earlier"
        assert (inbox / "_duplicates" / "d" / "DJI_0002_2.JPG").read_bytes() == b"later"
    finally:
        conn.close()


def test_dismiss_missing_source_just_deletes_row(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    conn = _conn(tmp_path)
    try:
        rid = _record(conn, inbox / "gone.JPG")
        res = duplicates.dismiss(conn, [rid], inbox)
        assert res.dismissed == 1 and res.failures == []
        assert duplicates.list_rows(conn) == []
        assert not (inbox / "_duplicates").exists()  # nothing to move, nothing logged
    finally:
        conn.close()


def test_dismiss_unknown_id_skipped(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    conn = _conn(tmp_path)
    try:
        res = duplicates.dismiss(conn, [999], inbox)
        assert res.dismissed == 0 and res.skipped == 1 and res.failures == []
    finally:
        conn.close()


def test_dismiss_failure_is_isolated_per_item(tmp_path):
    # A row whose source is not under the inbox root (a moved config) fails its
    # relocation; the failure is recorded, the row stays, and the OTHER id still
    # dismisses. Null batch_id logs as an empty TSV field.
    inbox = tmp_path / "inbox"
    outside = _add(tmp_path, "elsewhere/DJI_9999.JPG")
    good = _add(inbox, "DJI_0001.JPG")
    conn = _conn(tmp_path)
    try:
        _seed_match(conn)
        bad_id = _record(conn, outside)
        good_id = _record(conn, good, batch_id=None)
        res = duplicates.dismiss(conn, [bad_id, good_id], inbox)
        assert res.dismissed == 1 and res.skipped == 0
        assert [f["id"] for f in res.failures] == [bad_id]
        assert res.failures[0]["error"]
        assert outside.exists()  # failed item untouched
        rows = duplicates.list_rows(conn)
        assert [r[0] for r in rows] == [bad_id]  # failed row kept for a retry
        line = (inbox / "_duplicates" / "duplicates.log").read_text(encoding="utf-8")
        assert line.strip().split("\t")[1] == ""  # null batch_id -> empty field
    finally:
        conn.close()


def test_dismiss_refuses_when_library_match_removed(tmp_path):
    # An undo since the last organize can delete the matched library row, making
    # this inbox file the ONLY remaining copy — relocating it would shelve it.
    # The file and the row stay put; the failure says to re-run organize.
    inbox = tmp_path / "inbox"
    prim = _add(inbox, "DJI_0003.JPG")
    conn = _conn(tmp_path)
    try:
        rid = _record(conn, prim)  # no files row seeded -> the match is gone
        res = duplicates.dismiss(conn, [rid], inbox)
        assert res.dismissed == 0 and res.skipped == 0
        assert res.failures == [{
            "id": rid,
            "error": "no longer a duplicate (library match was removed); "
                     "run organize to import it",
        }]
        assert prim.exists()  # untouched on disk
        assert [r[0] for r in duplicates.list_rows(conn)] == [rid]  # row kept
        assert not (inbox / "_duplicates").exists()  # nothing moved, nothing logged
    finally:
        conn.close()


def test_dismiss_missing_primary_relocates_surviving_companions(tmp_path):
    # The primary can vanish while a stored companion survives; the survivor
    # still moves (relocate_group skips missing files) and the row drains.
    inbox = tmp_path / "inbox"
    dng = _add(inbox, "card/DJI_0004.DNG", b"raw")
    prim = inbox / "card" / "DJI_0004.JPG"  # never on disk
    conn = _conn(tmp_path)
    try:
        _seed_match(conn)
        rid = _record(conn, prim, [dng])
        res = duplicates.dismiss(conn, [rid], inbox)
        assert res.dismissed == 1 and res.failures == []
        assert not dng.exists()
        assert (inbox / "_duplicates" / "card" / "DJI_0004.DNG").read_bytes() == b"raw"
        assert duplicates.list_rows(conn) == []
    finally:
        conn.close()


def test_dismiss_logs_live_dest_path(tmp_path):
    # A retag can move the matched file after the snapshot was recorded; the log
    # line names where the surviving copy actually lives NOW, not the snapshot.
    inbox = tmp_path / "inbox"
    prim = _add(inbox, "DJI_0005.JPG")
    conn = _conn(tmp_path)
    try:
        _seed_match(conn, dest="C:/lib/NEW/x.JPG")
        rid = _record(conn, prim, matched_dest="C:/lib/OLD/x.JPG")
        res = duplicates.dismiss(conn, [rid], inbox)
        assert res.dismissed == 1
        log = (inbox / "_duplicates" / "duplicates.log").read_text(encoding="utf-8")
        assert log.strip().split("\t")[3] == "C:/lib/NEW/x.JPG"
    finally:
        conn.close()
