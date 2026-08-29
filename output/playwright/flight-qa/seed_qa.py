from pathlib import Path

from geosorter import db


ROOT = Path("X:/src/geosorter-fable5/output/playwright/flight-qa")
LIBRARY = ROOT / "library" / "Boulder" / "2026-08-28"

conn = db.connect(ROOT / "index.db", integrity_check=False)
db.init_index_schema(conn)

rows = [
    ("DJI_0001.mp4", "video", "2026-08-28T14:10:00-06:00", "h264", 1.0, 40.010, -105.270),
    ("DJI_0002.mp4", "video", "2026-08-28T14:10:02-06:00", "h264", 1.0, 40.025, -105.250),
    ("DJI_0003.mp4", "video", "2026-08-28T14:10:04-06:00", "h264", 1.0, 40.020, -105.230),
    ("DJI_0100.mp4", "video", "2026-08-28T15:00:00-06:00", "h264", 1.0, 40.000, -105.280),
    ("DJI_0200.mp4", "video", "2026-08-28T16:00:00-06:00", "h264", None, 40.000, -105.300),
    ("DJI_PHOTO.jpg", "photo", "2026-08-28T17:00:00-06:00", None, None, 40.015, -105.255),
]

ids = {}
for filename, media_type, captured, codec, duration, lat, lon in rows:
    cur = conn.execute(
        "INSERT INTO files(geonameid, place_string, dest_path, filename, media_type, "
        "local_date, capture_ts_local, lat, lon, codec, duration_s, gps_source, sha256, "
        "status, capture_kind, frame_count, star_rating, stitch_status, stitch_projection) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            1,
            "Boulder, Colorado, United States",
            str(LIBRARY / filename),
            filename,
            media_type,
            "2026-08-28",
            captured,
            lat,
            lon,
            codec,
            duration,
            "srt" if filename in {"DJI_0001.mp4", "DJI_0002.mp4", "DJI_0200.mp4"} else "exif",
            f"qa-{filename}",
            "organized",
            None,
            None,
            None,
            None,
            None,
        ),
    )
    ids[filename] = cur.lastrowid

for filename in ("DJI_0001.mp4", "DJI_0002.mp4", "DJI_0200.mp4"):
    conn.execute(
        "INSERT INTO file_companions(primary_file_id, dest_path, companion_type) VALUES (?,?,?)",
        (ids[filename], str(LIBRARY / filename.replace(".mp4", ".SRT")), "srt"),
    )

conn.commit()
conn.close()
