"""Tests for the FastAPI backend (GeoJSON feed, media serving, organize jobs)."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from geosorter import api, db
from geosorter.config import Config

MEDIA = Path(__file__).parent / "fixtures" / "media"


def _probe_codec(path: Path) -> str:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    return out.stdout.strip()


def _seed(conn, *, dest_path, filename, media_type, status, lat, lon, codec=None):
    conn.execute(
        "INSERT INTO files(geonameid, place_string, dest_path, filename, media_type, "
        "local_date, lat, lon, codec, sha256, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (1, "Boulder, Colorado, United States", dest_path, filename, media_type,
         "2024-07-04", lat, lon, codec, "deadbeef", status),
    )


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
    )
    conn = db.connect(index_db, integrity_check=False)
    db.init_index_schema(conn)
    _seed(conn, dest_path=str(library / "A" / "a.JPG"), filename="a.JPG",
          media_type="photo", status="organized", lat=40.0, lon=-105.0)
    _seed(conn, dest_path=str(library / "B" / "b.JPG"), filename="b.JPG",
          media_type="photo", status="organized", lat=41.0, lon=-106.0)
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


def test_cancel_routes_are_partitioned_by_job_kind(client_and_lib):
    # A cancel route must not accept the other kind's job id (job ids never migrate
    # between the organize and undo tables, so this is timing-independent).
    client, _ = client_and_lib
    undo_id = client.post("/api/undo").json()["job_id"]
    org_id = client.post("/api/organize").json()["job_id"]
    assert client.post(f"/api/organize/cancel/{undo_id}").status_code == 404
    assert client.post(f"/api/undo/cancel/{org_id}").status_code == 404
