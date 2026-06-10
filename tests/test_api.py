"""Tests for the FastAPI backend (GeoJSON feed, media serving, organize jobs)."""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from geosorter import api, db, derived, pathing
from geosorter.config import Config
from geosorter.jobs import JobManager
from geosorter.organize import BatchReport

MEDIA = Path(__file__).parent / "fixtures" / "media"


def _probe_codec(path: Path) -> str:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    return out.stdout.strip()


def _seed(conn, *, dest_path, filename, media_type, status, lat, lon, codec=None,
          gps_source="exif", capture_kind=None, frame_count=None, star_rating=None,
          stitch_status=None):
    cur = conn.execute(
        "INSERT INTO files(geonameid, place_string, dest_path, filename, media_type, "
        "local_date, lat, lon, codec, gps_source, sha256, status, capture_kind, "
        "frame_count, star_rating, stitch_status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, "Boulder, Colorado, United States", dest_path, filename, media_type,
         "2024-07-04", lat, lon, codec, gps_source, "deadbeef", status,
         capture_kind, frame_count, star_rating, stitch_status),
    )
    return cur.lastrowid


@pytest.fixture
def client_and_lib(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    index_db = tmp_path / "index.db"
    cfg = Config(
        inbox_path=inbox,
        library_root=library,
        index_db_path=index_db,
        geonames_db_path=tmp_path / "geonames.db",
        spatial_index="rtree",
        cache_dir=tmp_path / "cache",  # local tier off library_root (no real-cache pollution)
    )
    conn = db.connect(index_db, integrity_check=False)
    db.init_index_schema(conn)
    _seed(conn, dest_path=str(library / "A" / "a.JPG"), filename="a.JPG",
          media_type="photo", status="organized", lat=40.0, lon=-105.0)
    _seed(conn, dest_path=str(library / "B" / "b.JPG"), filename="b.JPG",
          media_type="photo", status="organized", lat=41.0, lon=-106.0,
          gps_source="inferred")
    _seed(conn, dest_path=str(library / "_no-gps" / "q.JPG"), filename="q.JPG",
          media_type="photo", status="quarantined", lat=None, lon=None)
    _seed(conn, dest_path=str(library / "clips" / "v.mp4"), filename="v.mp4",
          media_type="video", status="organized", lat=42.0, lon=-107.0, codec="h265")
    conn.commit()
    conn.close()
    return TestClient(api.create_app(cfg)), library


def test_inbox_count_empty(client_and_lib):
    client, _ = client_and_lib  # fixture inbox is created empty
    resp = client.get("/api/inbox")
    assert resp.status_code == 200
    assert resp.json() == {"files": 0, "captures": 0}


def test_inbox_count_populated(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "DJI_0001.JPG").write_bytes(b"x")
    (inbox / "notes.txt").write_bytes(b"y")  # non-DJI: a file, not a capture
    cfg = Config(
        inbox_path=inbox,
        library_root=tmp_path / "library",
        index_db_path=tmp_path / "index.db",
        geonames_db_path=tmp_path / "geonames.db",
        spatial_index="rtree",
    )
    client = TestClient(api.create_app(cfg))
    assert client.get("/api/inbox").json() == {"files": 2, "captures": 1}


def test_inbox_list(tmp_path):
    inbox = tmp_path / "inbox"
    (inbox / "DCIM").mkdir(parents=True)
    (inbox / "DCIM" / "DJI_0001.JPG").write_bytes(b"x")
    cfg = Config(
        inbox_path=inbox,
        library_root=tmp_path / "library",
        index_db_path=tmp_path / "index.db",
        geonames_db_path=tmp_path / "geonames.db",
        spatial_index="rtree",
    )
    client = TestClient(api.create_app(cfg))
    assert client.get("/api/inbox/list").json() == {
        "groups": [
            {
                "id": "DCIM/DJI_0001.JPG",
                "dir": "DCIM",
                "name": "DJI_0001.JPG",
                "capture_kind": None,
                "file_count": 1,
            }
        ]
    }


def test_organize_forwards_primaries(tmp_path, monkeypatch):
    # The /api/organize route maps the optional {primaries:[...]} body to
    # jobs.submit(selected_primaries=set|None); a missing body -> None (full import).
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    library = tmp_path / "library"
    library.mkdir()
    cfg = Config(
        inbox_path=inbox,
        library_root=library,
        index_db_path=tmp_path / "index.db",
        geonames_db_path=tmp_path / "geonames.db",
        spatial_index="rtree",
    )

    calls = []

    def _spy_submit(self, selected_primaries=None):
        calls.append(selected_primaries)
        return "fakeid"

    monkeypatch.setattr(api.JobManager, "submit", _spy_submit)
    client = TestClient(api.create_app(cfg))

    assert client.post("/api/organize", json={"primaries": ["a", "b"]}).json() == {
        "job_id": "fakeid"
    }
    assert calls[-1] == {"a", "b"}

    assert client.post("/api/organize").json() == {"job_id": "fakeid"}
    assert calls[-1] is None


def test_library_returns_geojson_excluding_quarantined(client_and_lib):
    client, _ = client_and_lib
    resp = client.get("/api/library")
    assert resp.status_code == 200
    fc = resp.json()
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 3  # a, b, v organized+geolocated; quarantined excluded
    names = {f["properties"]["filename"] for f in fc["features"]}
    assert names == {"a.JPG", "b.JPG", "v.mp4"}
    feat = next(f for f in fc["features"] if f["properties"]["filename"] == "a.JPG")
    assert feat["geometry"]["type"] == "Point"
    assert feat["geometry"]["coordinates"] == [-105.0, 40.0]  # [lon, lat]
    assert feat["properties"]["path"] == "A/a.JPG"


def test_library_exposes_gps_source(client_and_lib):
    # The map UI needs gps_source to render inferred-location markers distinctly.
    client, _ = client_and_lib
    fc = client.get("/api/library").json()
    by_name = {f["properties"]["filename"]: f["properties"] for f in fc["features"]}
    assert by_name["a.JPG"]["gps_source"] == "exif"
    assert by_name["b.JPG"]["gps_source"] == "inferred"


def test_library_is_gzipped_for_accepting_client(client_and_lib):
    # The /api/library JSON is gzip-compressed when the client accepts it (the
    # video route stays untouched — gzip is scoped to this route, not a global
    # middleware). httpx transparently decodes, so .json() still parses.
    client, _ = client_and_lib
    resp = client.get("/api/library", headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") == "gzip"
    assert resp.json()["type"] == "FeatureCollection"


def _library_client(tmp_path):
    """A client plus its index-DB path, so a test can mutate rows mid-session."""
    library = tmp_path / "library"
    library.mkdir()
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    index_db = tmp_path / "index.db"
    cfg = Config(
        inbox_path=inbox,
        library_root=library,
        index_db_path=index_db,
        geonames_db_path=tmp_path / "geonames.db",
        spatial_index="rtree",
        cache_dir=tmp_path / "cache",
    )
    conn = db.connect(index_db, integrity_check=False)
    db.init_index_schema(conn)
    _seed(conn, dest_path=str(library / "A" / "a.JPG"), filename="a.JPG",
          media_type="photo", status="organized", lat=40.0, lon=-105.0)
    conn.commit()
    conn.close()
    return TestClient(api.create_app(cfg)), index_db, library


def test_library_conditional_get_304_then_200(tmp_path):
    # An unchanged library answers If-None-Match with 304 (no body); inserting a
    # new geolocated row changes the ETag (keyed on MAX(id)+COUNT(*)) -> 200.
    client, index_db, library = _library_client(tmp_path)
    first = client.get("/api/library")
    etag = first.headers["etag"]
    assert etag and first.headers.get("last-modified")

    cached = client.get("/api/library", headers={"If-None-Match": etag})
    assert cached.status_code == 304
    assert cached.content == b""
    assert cached.headers["etag"] == etag

    conn = db.connect(index_db, integrity_check=False)
    _seed(conn, dest_path=str(library / "C" / "c.JPG"), filename="c.JPG",
          media_type="photo", status="organized", lat=42.0, lon=-107.0)
    conn.commit()
    conn.close()

    changed = client.get("/api/library", headers={"If-None-Match": etag})
    assert changed.status_code == 200
    assert changed.headers["etag"] != etag
    assert len(changed.json()["features"]) == 2


def test_library_etag_changes_on_in_place_update(tmp_path):
    # MAX(id)+COUNT(*) alone misses in-place row UPDATEs (retag moves lat/lon;
    # stitch flips stitch_status) — both leave id/count unchanged. The ETag folds
    # in lat/lon + stitch-status signals so a retag/stitch reload gets 200 (fresh
    # marker), not a stale 304.
    client, index_db, library = _library_client(tmp_path)
    etag1 = client.get("/api/library").headers["etag"]

    conn = db.connect(index_db, integrity_check=False)
    conn.execute("UPDATE files SET lat=?, lon=? WHERE filename='a.JPG'", (50.0, 5.0))
    conn.commit()
    conn.close()
    moved = client.get("/api/library", headers={"If-None-Match": etag1})
    assert moved.status_code == 200  # retag-style move is NOT a 304
    etag2 = moved.headers["etag"]
    assert etag2 != etag1
    assert moved.json()["features"][0]["geometry"]["coordinates"] == [5.0, 50.0]

    conn = db.connect(index_db, integrity_check=False)
    conn.execute("UPDATE files SET stitch_status='ok' WHERE filename='a.JPG'")
    conn.commit()
    conn.close()
    stitched = client.get("/api/library", headers={"If-None-Match": etag2})
    assert stitched.status_code == 200  # stitch_status flip is NOT a 304
    assert stitched.headers["etag"] != etag2


def test_video_codec_lookup_handles_long_path_prefix(tmp_path):
    # _lookup_codec resolves the codec by an indexed dest_path equality, matching
    # the stored Windows \\?\ long-path form (production) as well as the plain form.
    library = tmp_path / "library"
    (library / "clips").mkdir(parents=True)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    index_db = tmp_path / "index.db"
    cfg = Config(
        inbox_path=inbox,
        library_root=library,
        index_db_path=index_db,
        geonames_db_path=tmp_path / "geonames.db",
        spatial_index="rtree",
        cache_dir=tmp_path / "cache",
    )
    conn = db.connect(index_db, integrity_check=False)
    db.init_index_schema(conn)
    _seed(conn, dest_path="\\\\?\\" + str(library / "clips" / "v.mp4"),
          filename="v.mp4", media_type="video", status="organized",
          lat=42.0, lon=-107.0, codec="h265")
    conn.commit()
    conn.close()
    shutil.copy(MEDIA / "h265_tiny.mp4", library / "clips" / "v.mp4")
    client = TestClient(api.create_app(cfg))
    resp = client.get("/api/video/clips/v.mp4")
    assert resp.status_code == 200
    out = tmp_path / "served.mp4"
    out.write_bytes(resp.content)
    assert _probe_codec(out) == "h264"  # HEVC source -> proxy via indexed codec lookup


def _hyperlapse_client(tmp_path):
    """A client whose library holds one hyperlapse render + 3 frame companions."""
    library = tmp_path / "library"
    library.mkdir()
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    index_db = tmp_path / "index.db"
    cfg = Config(
        inbox_path=inbox,
        library_root=library,
        index_db_path=index_db,
        geonames_db_path=tmp_path / "geonames.db",
        spatial_index="rtree",
    )
    conn = db.connect(index_db, integrity_check=False)
    db.init_index_schema(conn)
    fid = _seed(conn, dest_path=str(library / "P" / "hl.MP4"), filename="hl.MP4",
                media_type="video", status="organized", lat=4.81, lon=-75.68,
                gps_source="hyperlapse_frame", capture_kind="hyperlapse", frame_count=3)
    for i in range(1, 4):
        conn.execute(
            "INSERT INTO file_companions(primary_file_id, dest_path, companion_type) "
            "VALUES (?,?,?)",
            (fid, str(library / "P" / "hl_frames" / f"HYPERLAPSE_{i:04d}.JPG"),
             "hyperlapse_frame"),
        )
    conn.commit()
    conn.close()
    return TestClient(api.create_app(cfg)), fid


def test_library_exposes_capture_kind_and_frame_count(tmp_path):
    client, _ = _hyperlapse_client(tmp_path)
    fc = client.get("/api/library").json()
    feat = next(f for f in fc["features"] if f["properties"]["filename"] == "hl.MP4")
    assert feat["properties"]["capture_kind"] == "hyperlapse"
    assert feat["properties"]["frame_count"] == 3
    assert feat["properties"]["gps_source"] == "hyperlapse_frame"


def test_frames_lists_hyperlapse_companions(tmp_path):
    client, fid = _hyperlapse_client(tmp_path)
    resp = client.get(f"/api/frames/{fid}")
    assert resp.status_code == 200
    assert resp.json() == {
        "frames": [
            "P/hl_frames/HYPERLAPSE_0001.JPG",
            "P/hl_frames/HYPERLAPSE_0002.JPG",
            "P/hl_frames/HYPERLAPSE_0003.JPG",
        ]
    }


def test_frames_unknown_id_404(tmp_path):
    client, _ = _hyperlapse_client(tmp_path)
    assert client.get("/api/frames/999999").status_code == 404


def _panorama_client(tmp_path):
    """A client whose library holds one panorama primary + 2 tile companions."""
    library = tmp_path / "library"
    library.mkdir()
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    index_db = tmp_path / "index.db"
    cfg = Config(
        inbox_path=inbox,
        library_root=library,
        index_db_path=index_db,
        geonames_db_path=tmp_path / "geonames.db",
        spatial_index="rtree",
    )
    conn = db.connect(index_db, integrity_check=False)
    db.init_index_schema(conn)
    fid = _seed(conn, dest_path=str(library / "P" / "PANO_0001.JPG"),
                filename="PANO_0001.JPG", media_type="photo", status="organized",
                lat=4.81, lon=-75.68, gps_source="exif", capture_kind="panorama",
                frame_count=2)
    for i in range(2, 4):
        conn.execute(
            "INSERT INTO file_companions(primary_file_id, dest_path, companion_type) "
            "VALUES (?,?,?)",
            (fid, str(library / "P" / "PANO_0001_frames" / f"PANO_{i:04d}.JPG"),
             "panorama_frame"),
        )
    conn.commit()
    conn.close()
    return TestClient(api.create_app(cfg)), fid


def test_library_exposes_panorama_capture_kind(tmp_path):
    client, _ = _panorama_client(tmp_path)
    fc = client.get("/api/library").json()
    feat = next(
        f for f in fc["features"] if f["properties"]["filename"] == "PANO_0001.JPG"
    )
    assert feat["properties"]["capture_kind"] == "panorama"
    assert feat["properties"]["frame_count"] == 2
    assert feat["properties"]["gps_source"] == "exif"


def test_frames_lists_panorama_companions(tmp_path):
    client, fid = _panorama_client(tmp_path)
    resp = client.get(f"/api/frames/{fid}")
    assert resp.status_code == 200
    assert resp.json() == {
        "frames": [
            "P/PANO_0001_frames/PANO_0002.JPG",
            "P/PANO_0001_frames/PANO_0003.JPG",
        ]
    }


def test_library_exposes_star_rating(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    index_db = tmp_path / "index.db"
    cfg = Config(
        inbox_path=tmp_path / "inbox",
        library_root=library,
        index_db_path=index_db,
        geonames_db_path=tmp_path / "geonames.db",
        spatial_index="rtree",
    )
    conn = db.connect(index_db, integrity_check=False)
    db.init_index_schema(conn)
    _seed(conn, dest_path=str(library / "A" / "rated.JPG"), filename="rated.JPG",
          media_type="photo", status="organized", lat=40.0, lon=-105.0, star_rating=4)
    _seed(conn, dest_path=str(library / "B" / "unrated.JPG"), filename="unrated.JPG",
          media_type="photo", status="organized", lat=41.0, lon=-106.0, star_rating=None)
    conn.commit()
    conn.close()
    client = TestClient(api.create_app(cfg))
    by_name = {
        f["properties"]["filename"]: f["properties"]
        for f in client.get("/api/library").json()["features"]
    }
    assert by_name["rated.JPG"]["star_rating"] == 4
    assert by_name["unrated.JPG"]["star_rating"] is None


# --------------------------------------------------------------------------- #
# Panorama stitch (B13): GeoJSON stitch_status, the cached-hero serve route, and
# the POST-start + status-poll job plumbing.
def _panorama_stitch_cfg(tmp_path, *, stitch_status=None):
    library = tmp_path / "library"
    (library / "P").mkdir(parents=True)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    index_db = tmp_path / "index.db"
    cfg = Config(
        inbox_path=inbox,
        library_root=library,
        index_db_path=index_db,
        geonames_db_path=tmp_path / "geonames.db",
        spatial_index="rtree",
    )
    conn = db.connect(index_db, integrity_check=False)
    db.init_index_schema(conn)
    fid = _seed(conn, dest_path=str(library / "P" / "PANO_0001.JPG"),
                filename="PANO_0001.JPG", media_type="photo", status="organized",
                lat=4.81, lon=-75.68, gps_source="exif", capture_kind="panorama",
                frame_count=2, stitch_status=stitch_status)
    conn.commit()
    conn.close()
    return cfg, fid, library


def test_library_exposes_stitch_status(tmp_path):
    cfg, _, _ = _panorama_stitch_cfg(tmp_path, stitch_status="ok")
    client = TestClient(api.create_app(cfg))
    feat = next(
        f for f in client.get("/api/library").json()["features"]
        if f["properties"]["filename"] == "PANO_0001.JPG"
    )
    assert feat["properties"]["stitch_status"] == "ok"


def test_stitch_image_served_when_cached(tmp_path):
    cfg, fid, library = _panorama_stitch_cfg(tmp_path)
    # Pre-place a cached hero exactly where the route looks for it: proxy_cache_dir
    # (None -> the RAW library_root, as the stitch generator uses) keyed on the
    # primary's library-relative path.
    rel_key = pathing.library_rel_key(library, str(library / "P" / "PANO_0001.JPG"))
    out = derived.stitch_cache_path(library, rel_key)
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (60, 30), "skyblue").save(out, "JPEG")
    client = TestClient(api.create_app(cfg))
    resp = client.get(f"/api/stitch/{fid}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/jpeg")


def test_stitch_image_404_when_not_generated(tmp_path):
    cfg, fid, _ = _panorama_stitch_cfg(tmp_path)
    client = TestClient(api.create_app(cfg))
    assert client.get(f"/api/stitch/{fid}").status_code == 404  # -> client uses gallery


def test_stitch_routes_404_for_non_panorama_and_unknown(client_and_lib):
    # The seeded a.JPG (id 1) is a normal photo, not a panorama.
    client, _ = client_and_lib
    assert client.get("/api/stitch/1").status_code == 404
    assert client.get("/api/stitch/999999").status_code == 404
    assert client.post("/api/stitch/1").status_code == 404
    assert client.post("/api/stitch/999999").status_code == 404
    # The route is file-id-keyed: an intermediate name like a .pto is unreachable
    # (no int file_id), so .pto / intermediates are never servable.
    assert client.get("/api/stitch/project.pto").status_code in (404, 422)


def test_stitch_post_starts_job_and_status_polls(tmp_path):
    cfg, fid, _ = _panorama_stitch_cfg(tmp_path)
    jm = JobManager(cfg, stitch_fn=lambda *a, **k: Path("stitched.jpg"))
    client = TestClient(api.create_app(cfg, job_manager=jm))
    resp = client.post(f"/api/stitch/{fid}")
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    st = None
    for _ in range(300):
        st = client.get(f"/api/stitch/status/{job_id}").json()
        if st["state"] in ("done", "error"):
            break
        time.sleep(0.02)
    assert st["state"] == "done"
    assert st["status"] == "ok"
    assert client.get("/api/stitch/status/does-not-exist").status_code == 404


def test_stitch_status_reports_step_progress(tmp_path):
    # The live Hugin step reported via on_step must reach the HTTP status payload, so
    # the map UI can show "step 3/6: cpclean" mid-run (regression guard for the
    # progress label that appeared stuck at 0/6).
    import threading

    cfg, fid, _ = _panorama_stitch_cfg(tmp_path)
    proceed = threading.Event()

    def fake(*a, on_step=None, **k):
        on_step(3, 6, "cpclean")
        proceed.wait(2.0)  # hold the job 'running' so the test can observe the step
        return Path("stitched.jpg")

    jm = JobManager(cfg, stitch_fn=fake)
    client = TestClient(api.create_app(cfg, job_manager=jm))
    job_id = client.post(f"/api/stitch/{fid}").json()["job_id"]

    seen = None
    for _ in range(300):
        seen = client.get(f"/api/stitch/status/{job_id}").json()
        if seen.get("step") == 3:
            break
        time.sleep(0.02)
    proceed.set()
    assert seen["step"] == 3
    assert seen["step_total"] == 6
    assert seen["step_name"] == "cpclean"
    assert seen["state"] == "running"


def test_media_db_extension_blocked(client_and_lib):
    # Catalog DBs live outside library_root, but belt-and-suspenders: never serve a
    # .db even if one is somehow under the library.
    client, library = client_and_lib
    (library / "catalog.db").write_bytes(b"SQLite format 3\x00")
    resp = client.get("/api/media/catalog.db")
    assert resp.status_code == 403


def test_media_range_request_returns_206(client_and_lib):
    client, library = client_and_lib
    (library / "clip.bin").write_bytes(b"0123456789")
    resp = client.get("/api/media/clip.bin", headers={"Range": "bytes=0-3"})
    assert resp.status_code == 206
    assert resp.content == b"0123"
    assert resp.headers["content-range"] == "bytes 0-3/10"


def test_media_path_traversal_blocked(client_and_lib):
    client, library = client_and_lib
    (library.parent / "secret.txt").write_text("TOPSECRET")
    resp = client.get("/api/media/..%2Fsecret.txt")
    assert resp.status_code in (403, 404)
    assert "TOPSECRET" not in resp.text


def test_thumb_endpoint_returns_jpeg(client_and_lib):
    client, library = client_and_lib
    shutil.copy(MEDIA / "dji_photo.jpg", library / "photo.jpg")
    resp = client.get("/api/thumb/photo.jpg")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/jpeg")


def test_preview_endpoint_returns_jpeg(client_and_lib):
    client, library = client_and_lib
    shutil.copy(MEDIA / "dji_photo.jpg", library / "photo.jpg")
    resp = client.get("/api/preview/photo.jpg")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/jpeg")


def test_thumb_served_from_local_cache_dir(client_and_lib):
    # Thumbnails live on the LOCAL cache_dir tier, never under the SMB library share.
    client, library = client_and_lib
    shutil.copy(MEDIA / "dji_photo.jpg", library / "photo.jpg")
    assert client.get("/api/thumb/photo.jpg").status_code == 200
    cache_dir = library.parent / "cache"  # client_and_lib's cfg cache_dir
    assert (cache_dir / ".geosorter-cache" / "thumbs").is_dir()
    assert not (library / ".geosorter-cache").exists()  # nothing written to the share


def test_safe_cache_path_rejects_path_outside_roots(tmp_path):
    # The defense-in-depth guard: a served derived path must live under a cache root.
    inside = tmp_path / "cache" / "f.jpg"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"x")
    assert api._safe_cache_path(inside, tmp_path / "cache") == inside  # under root -> ok
    outside = tmp_path / "elsewhere" / "f.jpg"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"x")
    with pytest.raises(HTTPException) as ei:
        api._safe_cache_path(outside, tmp_path / "cache")
    assert ei.value.status_code == 403


def test_spa_mounted_when_dir_exists(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    spa = tmp_path / "webui"
    spa.mkdir()
    (spa / "index.html").write_text("<!doctype html><title>geosorter</title>", encoding="utf-8")
    cfg = Config(
        inbox_path=tmp_path / "inbox",
        library_root=library,
        index_db_path=tmp_path / "index.db",
        geonames_db_path=tmp_path / "geonames.db",
        spatial_index="rtree",
    )
    client = TestClient(api.create_app(cfg, spa_dir=spa))
    resp = client.get("/")
    assert resp.status_code == 200
    assert "geosorter" in resp.text  # the mounted index.html is served at the origin


def test_video_endpoint_serves_h264_proxy_for_hevc(client_and_lib):
    client, library = client_and_lib
    (library / "clips").mkdir(parents=True, exist_ok=True)
    shutil.copy(MEDIA / "h265_tiny.mp4", library / "clips" / "v.mp4")
    resp = client.get("/api/video/clips/v.mp4")
    assert resp.status_code == 200
    out = library.parent / "served.mp4"
    out.write_bytes(resp.content)
    assert _probe_codec(out) == "h264"  # HEVC source transcoded to a playable proxy


def test_organize_job_lifecycle(client_and_lib):
    client, _ = client_and_lib
    resp = client.post("/api/organize")
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    st = None
    for _ in range(300):
        st = client.get(f"/api/organize/status/{job_id}").json()
        if st["state"] in ("done", "error", "cancelled"):
            break
        time.sleep(0.02)
    assert st["state"] == "done"  # empty inbox -> clean completion
    assert st["organized"] == 0
    # The status payload surfaces the plan totals + bytes-based ETA (empty inbox -> 0/None).
    assert st["total_groups"] == 0
    assert st["total_bytes"] == 0
    assert "eta_seconds" in st

    assert client.get("/api/organize/status/does-not-exist").status_code == 404
    assert client.post("/api/organize/cancel/does-not-exist").status_code == 404


def test_undo_job_lifecycle(client_and_lib):
    client, _ = client_and_lib
    resp = client.post("/api/undo")
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    st = None
    for _ in range(300):
        st = client.get(f"/api/undo/status/{job_id}").json()
        if st["state"] in ("done", "error", "cancelled"):
            break
        time.sleep(0.02)
    assert st["state"] == "done"  # no moves rows seeded -> nothing to undo
    assert st["nothing_to_undo"] is True
    assert st["restored"] == 0

    assert client.get("/api/undo/status/does-not-exist").status_code == 404
    assert client.post("/api/undo/cancel/does-not-exist").status_code == 404


def test_retag_job_lifecycle(client_and_lib):
    # An unknown file_id exercises the route + job plumbing without needing a real
    # geonames DB or on-disk media (retag_file returns 'not_found' before geocoding).
    client, _ = client_and_lib
    resp = client.post("/api/retag", json={"file_id": 99999, "lat": 39.7, "lon": -104.9})
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    st = None
    for _ in range(300):
        st = client.get(f"/api/retag/status/{job_id}").json()
        if st["state"] in ("done", "error"):
            break
        time.sleep(0.02)
    assert st["state"] == "done"
    assert st["status"] == "not_found"
    assert st["moved"] == 0

    assert client.get("/api/retag/status/does-not-exist").status_code == 404


def test_rescan_job_lifecycle(client_and_lib):
    # The fixture seeds 4 files rows but writes no files on disk, so a rescan finds
    # all four dest_paths missing and prunes them — exercising the route + job + the
    # real run_rescan end to end.
    client, _ = client_and_lib
    resp = client.post("/api/rescan")
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    st = None
    for _ in range(300):
        st = client.get(f"/api/rescan/status/{job_id}").json()
        if st["state"] in ("done", "error"):
            break
        time.sleep(0.02)
    assert st["state"] == "done"
    assert st["checked"] == 4
    assert st["pruned"] == 4
    assert st["kept"] == 0

    # The map feed is now empty (all phantom rows pruned).
    assert client.get("/api/library").json()["features"] == []

    assert client.get("/api/rescan/status/does-not-exist").status_code == 404


def test_undo_retag_rescan_return_409_while_organize_running(tmp_path):
    # The single destructive worker is shared; submitting undo/retag/rescan while a
    # multi-hour organize holds it returns 409 + the blocking job id (no silent queue).
    library = tmp_path / "library"
    library.mkdir()
    cfg = Config(
        inbox_path=tmp_path / "inbox",
        library_root=library,
        index_db_path=tmp_path / "index.db",
        geonames_db_path=tmp_path / "geonames.db",
        spatial_index="rtree",
    )
    block = threading.Event()

    def slow_organize(cfg, *, assume_yes, cancel, progress, byte_progress,
                      selected_primaries=None, on_plan=None):
        block.wait(3.0)
        return BatchReport(batch_id="x")

    jm = JobManager(cfg, organize_fn=slow_organize)
    client = TestClient(api.create_app(cfg, job_manager=jm))
    try:
        org_id = client.post("/api/organize").json()["job_id"]
        undo = client.post("/api/undo")
        assert undo.status_code == 409
        assert undo.json()["detail"]["blocking_job_id"] == org_id
        assert client.post(
            "/api/retag", json={"file_id": 1, "lat": 0.0, "lon": 0.0}
        ).status_code == 409
        assert client.post("/api/rescan").status_code == 409
    finally:
        block.set()


def test_cancel_routes_are_partitioned_by_job_kind(client_and_lib):
    # A cancel route must not accept the other kind's job id (job ids never migrate
    # between the organize and undo tables, so this is timing-independent).
    client, _ = client_and_lib
    undo_id = client.post("/api/undo").json()["job_id"]
    org_id = client.post("/api/organize").json()["job_id"]
    assert client.post(f"/api/organize/cancel/{undo_id}").status_code == 404
    assert client.post(f"/api/undo/cancel/{org_id}").status_code == 404


def test_relpath_prefers_matching_root(tmp_path):
    # A non-matching root is passed first, the matching root second: _relpath must
    # try each and return the correct library-relative path (the mapped-drive case,
    # where the resolved/UNC root does not match the stored Z:\ form but the raw
    # root does).
    dest = str(tmp_path / "lib" / "Place" / "f.JPG")
    assert api._relpath(dest, tmp_path / "OTHER", tmp_path / "lib") == "Place/f.JPG"
    assert api._relpath(dest, tmp_path / "lib") == "Place/f.JPG"


def test_relpath_warns_and_falls_back_when_no_root_matches(tmp_path, caplog):
    dest = str(tmp_path / "a" / "b.JPG")
    with caplog.at_level("WARNING", logger="geosorter.api"):
        result = api._relpath(dest, tmp_path / "x")
    assert result == "b.JPG"
    assert any(rec.levelname == "WARNING" for rec in caplog.records)


def test_library_path_roundtrips_when_root_resolves_differently(tmp_path):
    # Reproduce the mapped-drive case end to end: cfg.library_root resolves to a
    # DIFFERENT path than the stored dest_path form (Z:\ -> UNC in production; a
    # directory symlink here). Without the raw-root fallback the relpath degrades to
    # a bare filename and /api/thumb 404s.
    real = tmp_path / "real_lib"
    (real / "Place" / "2024-01-01").mkdir(parents=True)
    link = tmp_path / "link_lib"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks not permitted on this platform")
    if Path(link).resolve() == link:
        pytest.skip("symlink did not change resolve() on this platform")

    img = link / "Place" / "2024-01-01" / "p.JPG"
    shutil.copy(MEDIA / "dji_photo.jpg", img)
    (tmp_path / "inbox").mkdir()
    index_db = tmp_path / "index.db"
    cfg = Config(
        inbox_path=tmp_path / "inbox",
        library_root=link,  # raw form differs from link.resolve() (== real)
        index_db_path=index_db,
        geonames_db_path=tmp_path / "geonames.db",
        spatial_index="rtree",
    )
    conn = db.connect(index_db, integrity_check=False)
    db.init_index_schema(conn)
    _seed(conn, dest_path=str(img), filename="p.JPG", media_type="photo",
          status="organized", lat=40.0, lon=-105.0)
    conn.commit()
    conn.close()

    client = TestClient(api.create_app(cfg))
    path = client.get("/api/library").json()["features"][0]["properties"]["path"]
    assert path == "Place/2024-01-01/p.JPG"  # folderful, not the bare "p.JPG"
    assert client.get(f"/api/thumb/{path}").status_code == 200
