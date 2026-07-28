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

from geosorter import api, db, derived, duplicates, pathing
from geosorter.config import Config
from geosorter.jobs import JobManager
from geosorter.organize import BatchReport
from geosorter.setloc import AssignReport

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
          stitch_status=None, stitch_projection=None,
          capture_ts_local="2024-07-04T13:05:00-06:00", sha256="deadbeef"):
    cur = conn.execute(
        "INSERT INTO files(geonameid, place_string, dest_path, filename, media_type, "
        "local_date, capture_ts_local, lat, lon, codec, gps_source, sha256, status, "
        "capture_kind, frame_count, star_rating, stitch_status, stitch_projection) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, "Boulder, Colorado, United States", dest_path, filename, media_type,
         "2024-07-04", capture_ts_local, lat, lon, codec, gps_source, sha256,
         status, capture_kind, frame_count, star_rating, stitch_status,
         stitch_projection),
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


def test_quarantine_lists_only_quarantined(client_and_lib):
    # The fixture seeds three organized rows and one quarantined (_no-gps/q.JPG).
    client, _ = client_and_lib
    resp = client.get("/api/quarantine")
    assert resp.status_code == 200
    feats = resp.json()["features"]
    assert len(feats) == 1
    only = feats[0]
    assert only["filename"] == "q.JPG"
    assert only["media_type"] == "photo"
    assert only["path"] == "_no-gps/q.JPG"
    assert only["date"] == "2024-07-04"  # local_date present in the seed


def _geonames_app(tmp_path):
    from geosorter import geonames_loader

    gn = tmp_path / "geonames.db"
    geonames_loader.load(gn, Path(__file__).parent / "fixtures" / "geonames",
                         spatial_index="rtree")
    cfg = Config(
        inbox_path=tmp_path / "inbox",
        library_root=tmp_path / "library",
        index_db_path=tmp_path / "index.db",
        geonames_db_path=gn,
        spatial_index="rtree",
    )
    (tmp_path / "library").mkdir()
    return TestClient(api.create_app(cfg))


def test_place_search_returns_matches(tmp_path):
    client = _geonames_app(tmp_path)
    resp = client.get("/api/place-search", params={"q": "Denver"})
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results
    top = results[0]
    assert top["geonameid"] == 5419384
    assert top["name"] == "Denver"
    assert top["place_string"] == "Denver, Colorado, United States"
    assert round(top["lat"], 3) == 39.739
    assert round(top["lon"], 3) == -104.985


def test_place_search_blank_empty(client_and_lib):
    client, _ = client_and_lib
    assert client.get("/api/place-search", params={"q": "   "}).json() == {"results": []}


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


def test_library_exposes_capture_ts_local(client_and_lib):
    # The lightbox caption needs the local capture timestamp (date + time); the
    # feed forwards the stored ISO-8601-with-offset string verbatim.
    client, _ = client_and_lib
    fc = client.get("/api/library").json()
    feat = next(f for f in fc["features"] if f["properties"]["filename"] == "a.JPG")
    assert feat["properties"]["capture_ts_local"] == "2024-07-04T13:05:00-06:00"


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
    etag3 = stitched.headers["etag"]
    assert etag3 != etag2

    # A stitch_projection change (e.g. a cache-hit backfill) with status already 'ok'
    # leaves id/count/status-sum unchanged — the projection fold must still flip the ETag.
    conn = db.connect(index_db, integrity_check=False)
    conn.execute("UPDATE files SET stitch_projection='flat' WHERE filename='a.JPG'")
    conn.commit()
    conn.close()
    reproj = client.get("/api/library", headers={"If-None-Match": etag3})
    assert reproj.status_code == 200  # stitch_projection flip is NOT a 304
    assert reproj.headers["etag"] != etag3


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


SRT_FIXTURES = Path(__file__).parent / "fixtures" / "srt"


def _track_client(tmp_path, *, with_srt=True):
    """A client whose library holds one video, optionally with an SRT sidecar."""
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
    fid = _seed(conn, dest_path=str(library / "P" / "v.MP4"), filename="v.MP4",
                media_type="video", status="organized", lat=14.79, lon=-65.69,
                gps_source="srt", codec="h264")
    if with_srt:
        srt_dest = library / "P" / "v.SRT"
        srt_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(SRT_FIXTURES / "mini4pro_bracket.srt", srt_dest)
        conn.execute(
            "INSERT INTO file_companions(primary_file_id, dest_path, companion_type) "
            "VALUES (?,?,?)",
            (fid, str(srt_dest), "srt"),
        )
    conn.commit()
    conn.close()
    return TestClient(api.create_app(cfg)), fid


def test_track_returns_lon_lat_points(tmp_path):
    client, fid = _track_client(tmp_path)
    resp = client.get(f"/api/track/{fid}")
    assert resp.status_code == 200
    payload = resp.json()
    points = payload["points"]
    # The fixture's first frame is pre-lock (0,0) and must be skipped; the valid
    # fixes are around (lat 14.798, lon -65.691) — points are [lon, lat].
    assert len(points) >= 2
    for lon, lat in points:
        assert -66.0 < lon < -65.0
        assert 14.0 < lat < 15.0
    assert payload["samples"] == [
        {"time_s": 0.033, "lon": -65.69145, "lat": 14.79824, "alt": 0.0},
        {"time_s": 0.066, "lon": -65.691449, "lat": 14.79824, "alt": 0.0},
    ]
    # The fixture's `rel_alt` is height above the takeoff point.
    assert payload["altitude_ref"] == "relative"


def test_track_altitude_ref_null_without_altitude_tokens(tmp_path, monkeypatch):
    # A sidecar with fixes but no height token still returns a usable track; the
    # UI reads the null ref as "no altitude readout", not "sea level".
    client, fid = _track_client(tmp_path)
    samples = [
        api.srt_parser.SrtTrackSample(time_s=float(i), lat=40.0 + i, lon=-105.0)
        for i in range(2)
    ]
    monkeypatch.setattr(api.srt_parser, "parse_srt_track_samples", lambda _path: samples)
    payload = client.get(f"/api/track/{fid}").json()
    assert payload["altitude_ref"] is None
    assert [s["alt"] for s in payload["samples"]] == [None, None]


def test_track_video_without_srt_is_empty(tmp_path):
    client, fid = _track_client(tmp_path, with_srt=False)
    resp = client.get(f"/api/track/{fid}")
    assert resp.status_code == 200
    assert resp.json() == {"points": [], "samples": [], "altitude_ref": None}


def test_track_unknown_id_404(tmp_path):
    client, _ = _track_client(tmp_path)
    assert client.get("/api/track/999999").status_code == 404


def test_track_downsampling_preserves_both_endpoints(tmp_path, monkeypatch):
    client, fid = _track_client(tmp_path)
    fixes = [(float(i), float(-i)) for i in range(600)]
    samples = [
        api.srt_parser.SrtTrackSample(
            time_s=float(i), lat=float(i), lon=float(-i), alt=float(i), alt_ref="relative"
        )
        for i in range(600)
    ]
    monkeypatch.setattr(api.srt_parser, "parse_srt_track", lambda _path: fixes)
    monkeypatch.setattr(api.srt_parser, "parse_srt_track_samples", lambda _path: samples)

    payload = client.get(f"/api/track/{fid}").json()
    assert len(payload["points"]) == 500
    assert payload["points"][0] == [0.0, 0.0]
    assert payload["points"][-1] == [-599.0, 599.0]
    assert len(payload["samples"]) == 500
    assert payload["samples"][0]["time_s"] == 0.0
    assert payload["samples"][-1] == {
        "time_s": 599.0,
        "lon": -599.0,
        "lat": 599.0,
        "alt": 599.0,
    }


def test_library_exposes_has_track(tmp_path):
    client, _ = _track_client(tmp_path)
    fc = client.get("/api/library").json()
    feat = next(f for f in fc["features"] if f["properties"]["filename"] == "v.MP4")
    assert feat["properties"]["has_track"] is True


def test_library_has_track_false_without_srt(tmp_path):
    client, _ = _track_client(tmp_path, with_srt=False)
    fc = client.get("/api/library").json()
    feat = next(f for f in fc["features"] if f["properties"]["filename"] == "v.MP4")
    assert feat["properties"]["has_track"] is False


# --- Duplicate review (GET /api/duplicates, POST /api/duplicates/dismiss) ---


def _duplicates_client(tmp_path):
    """A client with one on-disk pending duplicate and one whose source is gone."""
    library = tmp_path / "library"
    library.mkdir()
    inbox = tmp_path / "inbox"
    present = inbox / "card" / "DJI_0002.JPG"
    present.parent.mkdir(parents=True)
    present.write_bytes(b"dup-bytes")
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
    fid = _seed(conn, dest_path=str(library / "A" / "a.JPG"), filename="a.JPG",
                media_type="photo", status="organized", lat=40.0, lon=-105.0)
    duplicates.record(conn, source_path=str(present), sha256="deadbeef",
                      companion_paths=[], matched_file_id=fid,
                      matched_dest_path=str(library / "A" / "a.JPG"), batch_id="B1")
    duplicates.record(conn, source_path=str(inbox / "gone.JPG"), sha256="feedface",
                      companion_paths=[], matched_file_id=None,
                      matched_dest_path=None, batch_id=None)
    conn.commit()
    conn.close()
    return TestClient(api.create_app(cfg)), inbox, fid


def test_duplicates_list_shape(tmp_path):
    client, _inbox, fid = _duplicates_client(tmp_path)
    body = client.get("/api/duplicates").json()
    assert body["count"] == 2
    first, second = body["items"]
    assert first["filename"] == "DJI_0002.JPG"
    assert first["source_path"] == "card/DJI_0002.JPG"  # inbox-relative POSIX
    assert first["matched_path"] == "A/a.JPG"  # library-relative, like media URLs
    assert first["matched_file_id"] == fid
    assert first["sha256"] == "deadbeef"
    assert first["first_seen_at"]
    assert first["missing"] is False
    assert second["missing"] is True  # source gone from disk
    assert second["matched_path"] is None


def test_duplicates_dismiss_moves_files(tmp_path):
    client, inbox, _fid = _duplicates_client(tmp_path)
    ids = [i["id"] for i in client.get("/api/duplicates").json()["items"]]
    resp = client.post("/api/duplicates/dismiss", json={"ids": ids})
    assert resp.status_code == 200
    assert resp.json() == {"dismissed": 2, "skipped": 0, "failures": []}
    # The on-disk duplicate really moved (subpath preserved); the missing one
    # was drained by row deletion alone.
    assert (inbox / "_duplicates" / "card" / "DJI_0002.JPG").exists()
    assert not (inbox / "card" / "DJI_0002.JPG").exists()
    assert client.get("/api/duplicates").json()["count"] == 0
    # An unknown id is skipped, not an error; an empty list is a 422 client bug.
    assert client.post("/api/duplicates/dismiss", json={"ids": [999]}).json() == {
        "dismissed": 0, "skipped": 1, "failures": [],
    }
    assert client.post("/api/duplicates/dismiss", json={"ids": []}).status_code == 422


def test_duplicates_dismiss_409_while_organize_running(tmp_path):
    # Moving inbox files under a live organize is racy: the dismiss route answers
    # 409 + the blocking job id (same shape as the destructive-job submits).
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
                      selected_primaries=None, on_plan=None, invalidate=None):
        block.wait(3.0)
        return BatchReport(batch_id="x")

    jm = JobManager(cfg, organize_fn=slow_organize)
    client = TestClient(api.create_app(cfg, job_manager=jm))
    try:
        org_id = client.post("/api/organize").json()["job_id"]
        resp = client.post("/api/duplicates/dismiss", json={"ids": [1]})
        assert resp.status_code == 409
        assert resp.json()["detail"]["blocking_job_id"] == org_id
    finally:
        block.set()


def test_duplicates_dismiss_409_without_inbox(tmp_path):
    # No inbox_path configured -> a clean 409 (same detail shape as the busy 409,
    # null blocking_job_id), not a 500 from Path(None).
    library = tmp_path / "library"
    library.mkdir()
    cfg = Config(
        inbox_path=None,
        library_root=library,
        index_db_path=tmp_path / "index.db",
        geonames_db_path=tmp_path / "geonames.db",
        spatial_index="rtree",
        cache_dir=tmp_path / "cache",
    )
    client = TestClient(api.create_app(cfg))
    resp = client.post("/api/duplicates/dismiss", json={"ids": [1]})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["message"] == "no inbox_path configured; cannot relocate duplicates"
    assert detail["blocking_job_id"] is None


# --- Favorites (POST /api/favorite, /api/library is_favorite) ---


def _first_feature(client):
    return client.get("/api/library").json()["features"][0]["properties"]


def test_favorite_toggle_lifecycle(tmp_path):
    client, _index_db, _library = _library_client(tmp_path)
    props = _first_feature(client)
    fid = props["id"]
    assert props["is_favorite"] is False
    resp = client.post("/api/favorite", json={"file_id": fid, "favorite": True})
    assert resp.status_code == 200
    assert resp.json() == {"file_id": fid, "favorite": True}
    assert _first_feature(client)["is_favorite"] is True
    # Idempotent: favoriting an already-favorited file is a clean no-op.
    assert client.post(
        "/api/favorite", json={"file_id": fid, "favorite": True}
    ).status_code == 200
    assert _first_feature(client)["is_favorite"] is True
    resp = client.post("/api/favorite", json={"file_id": fid, "favorite": False})
    assert resp.json() == {"file_id": fid, "favorite": False}
    assert _first_feature(client)["is_favorite"] is False


def test_favorite_unknown_file_404(tmp_path):
    client, _index_db, _library = _library_client(tmp_path)
    resp = client.post("/api/favorite", json={"file_id": 999999, "favorite": True})
    assert resp.status_code == 404


def test_library_etag_flips_on_favorite_toggle(tmp_path):
    # A favorite toggle changes no files row (it is a favorites row), so the ETag
    # must fold in the favorites signal or the reload would wrongly 304.
    client, _index_db, _library = _library_client(tmp_path)
    first = client.get("/api/library")
    etag1 = first.headers["etag"]
    assert etag1.startswith('W/"lib4-')  # payload-schema token (v4: is_favorite)
    fid = first.json()["features"][0]["properties"]["id"]
    client.post("/api/favorite", json={"file_id": fid, "favorite": True})
    changed = client.get("/api/library", headers={"If-None-Match": etag1})
    assert changed.status_code == 200  # favorite toggle is NOT a 304
    assert changed.headers["etag"] != etag1
    assert changed.json()["features"][0]["properties"]["is_favorite"] is True


def test_library_etag_ignores_favorite_outside_payload(tmp_path):
    # The favorites fold ranges over the exact payload predicate (organized +
    # GPS): favoriting a quarantined file leaves the payload byte-identical, so
    # the ETag must NOT flip (a flip would bust every client's cache for
    # nothing) — while favoriting a payload file still must.
    client, index_db, library = _library_client(tmp_path)
    conn = db.connect(index_db, integrity_check=False)
    qid = _seed(conn, dest_path=str(library / "_no-gps" / "q.JPG"), filename="q.JPG",
                media_type="photo", status="quarantined", lat=None, lon=None,
                sha256="quarantined-sha")
    conn.commit()
    conn.close()
    etag1 = client.get("/api/library").headers["etag"]
    client.post("/api/favorite", json={"file_id": qid, "favorite": True})
    same = client.get("/api/library", headers={"If-None-Match": etag1})
    assert same.status_code == 304  # quarantined favorite is invisible to the map
    fid = client.get("/api/library").json()["features"][0]["properties"]["id"]
    client.post("/api/favorite", json={"file_id": fid, "favorite": True})
    changed = client.get("/api/library", headers={"If-None-Match": etag1})
    assert changed.status_code == 200  # payload favorite still flips the ETag
    assert changed.headers["etag"] != etag1


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
def _panorama_stitch_cfg(tmp_path, *, stitch_status=None, stitch_projection=None):
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
                frame_count=2, stitch_status=stitch_status,
                stitch_projection=stitch_projection)
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


def test_library_exposes_stitch_projection(tmp_path):
    cfg, _, _ = _panorama_stitch_cfg(
        tmp_path, stitch_status="ok", stitch_projection="flat"
    )
    client = TestClient(api.create_app(cfg))
    feat = next(
        f for f in client.get("/api/library").json()["features"]
        if f["properties"]["filename"] == "PANO_0001.JPG"
    )
    assert feat["properties"]["stitch_projection"] == "flat"


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
    jm = JobManager(cfg, stitch_fn=lambda *a, **k: derived.StitchResult(Path("stitched.jpg"), "equirectangular"))
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


def test_stitch_post_accepts_force_and_projection_body(tmp_path):
    # A manual re-stitch sends {force, projection}; both reach the stitch fn, and an
    # invalid projection is rejected by the pydantic model (422).
    cfg, fid, _ = _panorama_stitch_cfg(tmp_path)
    seen = {}

    def fake(*a, force=False, forced_projection=None, on_step=None, **k):
        seen["force"] = force
        seen["forced_projection"] = forced_projection
        return derived.StitchResult(Path("stitched.jpg"), "flat")

    jm = JobManager(cfg, stitch_fn=fake)
    client = TestClient(api.create_app(cfg, job_manager=jm))

    resp = client.post(
        f"/api/stitch/{fid}", json={"force": True, "projection": "cylindrical"}
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    for _ in range(300):
        if client.get(f"/api/stitch/status/{job_id}").json()["state"] in ("done", "error"):
            break
        time.sleep(0.02)
    assert seen["force"] is True
    assert seen["forced_projection"] == "cylindrical"

    # An unknown projection value is rejected before the job is submitted.
    bad = client.post(f"/api/stitch/{fid}", json={"projection": "fisheye"})
    assert bad.status_code == 422


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
        return derived.StitchResult(Path("stitched.jpg"), "equirectangular")

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


# --- Instant panorama collage (m-frontend-pano-ux) ------------------------- #


def _panorama_collage_cfg(tmp_path):
    """A panorama with a real primary tile + two frame tiles on disk + companion rows."""
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
        cache_dir=tmp_path / "cache",  # collage on the local tier, off the real cache
    )
    primary = library / "P" / "PANO_0001.JPG"
    Image.new("RGB", (200, 150), "green").save(primary, "JPEG")
    frame_dir = library / "P" / "PANO_0001_frames"
    frame_dir.mkdir()
    frames = []
    for i in (2, 3):
        f = frame_dir / f"PANO_{i:04d}.JPG"
        Image.new("RGB", (200, 150), "blue").save(f, "JPEG")
        frames.append(f)
    conn = db.connect(index_db, integrity_check=False)
    db.init_index_schema(conn)
    fid = _seed(conn, dest_path=str(primary), filename="PANO_0001.JPG",
                media_type="photo", status="organized", lat=4.81, lon=-75.68,
                gps_source="exif", capture_kind="panorama", frame_count=2)
    for f in frames:
        conn.execute(
            "INSERT INTO file_companions(primary_file_id, dest_path, companion_type) "
            "VALUES (?,?,?)", (fid, str(f), "panorama_frame"),
        )
    conn.commit()
    conn.close()
    return cfg, fid


def test_collage_served_for_panorama(tmp_path):
    cfg, fid = _panorama_collage_cfg(tmp_path)
    client = TestClient(api.create_app(cfg))
    resp = client.get(f"/api/collage/{fid}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/jpeg")


def test_collage_404_for_non_panorama_and_unknown(client_and_lib):
    client, _ = client_and_lib  # a.JPG (id 1) is a normal photo, not a panorama
    assert client.get("/api/collage/1").status_code == 404
    assert client.get("/api/collage/999999").status_code == 404


def test_media_db_extension_blocked(client_and_lib):
    # Catalog DBs live outside library_root, but belt-and-suspenders: never serve a
    # .db even if one is somehow under the library.
    client, library = client_and_lib
    (library / "catalog.db").write_bytes(b"SQLite format 3\x00")
    resp = client.get("/api/media/catalog.db")
    assert resp.status_code == 403


def test_media_cache_internals_blocked(client_and_lib):
    # With proxy_cache_dir == library_root (the default) the derived cache lives
    # under the library; /api/media must never serve its payload (proxies, .src
    # sidecars, stitch heroes) — including via a case-shifted dirname (NTFS).
    client, library = client_and_lib
    proxies = library / ".geosorter-cache" / "proxies"
    proxies.mkdir(parents=True)
    (proxies / "k.mp4").write_bytes(b"proxy-bytes")
    assert client.get("/api/media/.geosorter-cache/proxies/k.mp4").status_code == 403
    assert client.get("/api/media/.GEOSORTER-CACHE/proxies/k.mp4").status_code == 403


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


def test_assign_location_job_lifecycle(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    cfg = Config(
        inbox_path=tmp_path / "inbox",
        library_root=library,
        index_db_path=tmp_path / "index.db",
        geonames_db_path=tmp_path / "geonames.db",
        spatial_index="rtree",
    )

    def fake_assign(cfg, file_ids, lat, lon, *, progress):
        progress("  q.JPG")
        return AssignReport(assigned=len(file_ids), skipped=0,
                            place_string="Boulder, Colorado, United States")

    jm = JobManager(cfg, assign_fn=fake_assign)
    client = TestClient(api.create_app(cfg, job_manager=jm))
    resp = client.post(
        "/api/assign-location", json={"file_ids": [1, 2], "lat": 40.0, "lon": -105.0}
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    st = None
    for _ in range(300):
        st = client.get(f"/api/assign-location/status/{job_id}").json()
        if st["state"] in ("done", "error"):
            break
        time.sleep(0.02)
    assert st["state"] == "done"
    assert st["assigned"] == 2
    assert st["total"] == 2  # the selected count, set at submit, drives the progress UI
    assert st["place_string"] == "Boulder, Colorado, United States"

    # Out-of-range latitude is rejected by the pydantic model (422), not deep in the job.
    bad = client.post(
        "/api/assign-location", json={"file_ids": [1], "lat": 999.0, "lon": 0.0}
    )
    assert bad.status_code == 422
    assert client.get("/api/assign-location/status/does-not-exist").status_code == 404


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
                      selected_primaries=None, on_plan=None, invalidate=None):
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
        assert client.post(
            "/api/assign-location", json={"file_ids": [1], "lat": 0.0, "lon": 0.0}
        ).status_code == 409
        assert client.post("/api/rescan").status_code == 409
    finally:
        block.set()


def test_cancel_routes_are_partitioned_by_job_kind(client_and_lib):
    # A cancel route must not accept the other kind's job id (job ids never migrate
    # between the organize and undo tables).
    client, _ = client_and_lib
    undo_id = client.post("/api/undo").json()["job_id"]
    # Wait for the undo to settle: organize-submit now 409s while another
    # destructive kind is in flight (the cross-kind WorkerBusy guard).
    for _ in range(300):
        if client.get(f"/api/undo/status/{undo_id}").json()["state"] not in (
            "pending", "running",
        ):
            break
        time.sleep(0.02)
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


# --- Admin auth (m-implement-view-only-admin-auth) ---

from geosorter import auth as _auth  # noqa: E402

# A guarded mutating route that needs no request body and no real side effect to
# probe the gate (the rescan job runs in the background; we only assert the HTTP
# status of the submit).
_GUARDED = "/api/rescan"


def _password_client(tmp_path, password="s3cret"):
    """A TestClient whose Config has an admin password configured (cheap hash)."""
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
        admin_password_hash=_auth.hash_password(password, iterations=1),
    )
    conn = db.connect(index_db, integrity_check=False)
    db.init_index_schema(conn)
    conn.commit()
    conn.close()
    return TestClient(api.create_app(cfg))


def test_auth_status_required(tmp_path):
    client = _password_client(tmp_path)
    assert client.get("/api/auth").json() == {"auth_required": True}


def test_auth_status_not_required(client_and_lib):
    # The default fixture configures no password -> the app is open.
    client, _ = client_and_lib
    assert client.get("/api/auth").json() == {"auth_required": False}


def test_login_wrong_password_401(tmp_path):
    client = _password_client(tmp_path)
    assert client.post("/api/login", json={"password": "nope"}).status_code == 401


def test_login_ok_returns_token(tmp_path):
    client = _password_client(tmp_path)
    resp = client.post("/api/login", json={"password": "s3cret"})
    assert resp.status_code == 200
    assert isinstance(resp.json()["token"], str) and resp.json()["token"]


def test_login_when_not_configured_400(client_and_lib):
    client, _ = client_and_lib
    assert client.post("/api/login", json={"password": "x"}).status_code == 400


def test_login_throttled_after_repeated_failures(tmp_path):
    # A burst of wrong passwords flips /api/login from 401 to 429 (with a
    # Retry-After hint), and the throttle also blocks the CORRECT password
    # until the cooldown passes — online brute-force can't run at full speed.
    client = _password_client(tmp_path)
    for _ in range(5):
        assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    resp = client.post("/api/login", json={"password": "nope"})
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) >= 1
    assert client.post("/api/login", json={"password": "s3cret"}).status_code == 429


def test_guarded_route_401_without_token(tmp_path):
    client = _password_client(tmp_path)
    assert client.post(_GUARDED).status_code == 401
    # A bogus bearer is also rejected.
    assert client.post(
        _GUARDED, headers={"Authorization": "Bearer not-a-real-token"}
    ).status_code == 401


def test_guarded_route_ok_with_token(tmp_path):
    client = _password_client(tmp_path)
    token = client.post("/api/login", json={"password": "s3cret"}).json()["token"]
    resp = client.post(_GUARDED, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert "job_id" in resp.json()


def test_organize_route_is_guarded(tmp_path):
    # The main mutating route is gated too (not just rescan).
    client = _password_client(tmp_path)
    assert client.post("/api/organize").status_code == 401


def test_logout_invalidates_token(tmp_path):
    client = _password_client(tmp_path)
    token = client.post("/api/login", json={"password": "s3cret"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.post("/api/logout", headers=headers).status_code == 200
    assert client.post(_GUARDED, headers=headers).status_code == 401


def test_guarded_route_open_when_no_password(client_and_lib):
    # With no password configured the guard is a no-op: rescan submits without a token.
    client, _ = client_and_lib
    resp = client.post(_GUARDED)
    assert resp.status_code == 200
    assert "job_id" in resp.json()


# Every mutating route is guarded (the require_admin dependency runs before body
# validation, so even body-required routes 401 cleanly without a token). Each
# entry pairs the route with a valid JSON body (None = no body) so the
# open-without-password probe exercises the real handler, not just a 422.
_MUTATING_ROUTES = [
    ("/api/organize", None),
    ("/api/organize/cancel/x", None),
    ("/api/undo", None),
    ("/api/undo/cancel/x", None),
    ("/api/retag", None),
    ("/api/assign-location", None),
    ("/api/rescan", None),
    ("/api/stitch/1", None),
    ("/api/duplicates/dismiss", {"ids": [1]}),
    ("/api/favorite", {"file_id": 1, "favorite": True}),
]


@pytest.mark.parametrize("route,body", _MUTATING_ROUTES)
def test_all_mutating_routes_guarded(tmp_path, route, body):
    client = _password_client(tmp_path)
    assert client.post(route, json=body).status_code == 401


@pytest.mark.parametrize("route,body", _MUTATING_ROUTES)
def test_all_mutating_routes_open_without_password(client_and_lib, route, body):
    # With no password configured, none of them 401 (the guard is a no-op).
    client, _ = client_and_lib
    assert client.post(route, json=body).status_code != 401


# --- Corrupt / unrenderable media -> graceful response (m-fix-corrupt-media-graceful) ---

def test_poster_unrenderable_serves_placeholder(client_and_lib):
    client, library = client_and_lib
    bad = library / "clips" / "v.mp4"  # fixture seeds this as an h265 video row
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"\x00not a real mp4")
    resp = client.get("/api/poster/clips/v.mp4")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/jpeg")


def test_video_unrenderable_returns_422(client_and_lib):
    client, library = client_and_lib
    bad = library / "clips" / "v.mp4"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"\x00not a real mp4")
    resp = client.get("/api/video/clips/v.mp4")
    assert resp.status_code == 422
