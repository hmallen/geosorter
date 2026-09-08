"""Content-hash annotations; only currently public library videos are exposed."""

import sqlite3

from fastapi import HTTPException

ELIGIBLE = "media_type='video' AND status='organized' AND lat IS NOT NULL AND lon IS NOT NULL"


def video(conn: sqlite3.Connection, file_id: int) -> sqlite3.Row:
    row = conn.execute(f"SELECT * FROM files WHERE id=? AND {ELIGIBLE}", (file_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Video is not in the public library")
    return row


def list_markers(conn: sqlite3.Connection, file_id: int | None = None) -> list[dict]:
    # One representative per hash in the global list, but honor the selected file
    # for per-video reads. No FK: annotations survive undo and re-import.
    selected = video(conn, file_id) if file_id is not None else None
    rows = conn.execute(
        "SELECT m.id, f.id AS file_id, m.time_s, m.note, m.created_at, m.updated_at "
        "FROM video_markers m JOIN files f ON f.sha256=m.sha256 "
        f"WHERE f.id IN (SELECT {'id' if selected is not None else 'MIN(id)'} FROM files "
        f"WHERE {ELIGIBLE} "
        + ("AND id=?" if selected is not None else "GROUP BY sha256")
        + ") ORDER BY f.capture_ts_local DESC, f.id, m.time_s, m.id",
        (file_id,) if selected is not None else (),
    ).fetchall()
    return [dict(row) for row in rows]


def marker_video(conn: sqlite3.Connection, marker_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT f.* FROM files f JOIN video_markers m ON f.sha256=m.sha256 "
        f"WHERE m.id=? AND {ELIGIBLE} ORDER BY f.id LIMIT 1", (marker_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Marker is not in the public library")
    return row


def validate_time(row: sqlite3.Row, time_s: float) -> None:
    if row["duration_s"] is not None and time_s > row["duration_s"]:
        raise HTTPException(422, "Timestamp exceeds video duration")


def save(conn: sqlite3.Connection, file_id: int, time_s: float, note: str,
         marker_id: int | None = None) -> dict:
    row = video(conn, file_id)
    validate_time(row, time_s)
    if marker_id is None:
        marker_id = conn.execute(
            "INSERT INTO video_markers(sha256,time_s,note) VALUES (?,?,?)",
            (row["sha256"], time_s, note),
        ).lastrowid
    else:
        conn.execute(
            "UPDATE video_markers SET time_s=?,note=?,updated_at=datetime('now') WHERE id=?",
            (time_s, note, marker_id),
        )
    conn.commit()
    return next(m for m in list_markers(conn, file_id) if m["id"] == marker_id)
