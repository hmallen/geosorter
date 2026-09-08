"""Marker persistence, public visibility and the shared-admin boundary."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from geosorter import api, auth, db
from geosorter.config import Config


@pytest.fixture
def marker_library(tmp_path):
    cfg = Config(library_root=tmp_path / 'library', inbox_path=tmp_path / 'inbox',
                 index_db_path=tmp_path / 'index.db', geonames_db_path=tmp_path / 'geo.db',
                 cache_dir=tmp_path / 'cache')
    conn = db.connect(cfg.index_db_path)
    db.init_index_schema(conn)
    conn.execute("INSERT INTO files(id,dest_path,filename,media_type,status,sha256,lat,lon,duration_s) "
                 "VALUES (1,?,'clip.mp4','video','organized','video-hash',40,-105,60)",
                 (str(cfg.library_root / 'clip.mp4'),))
    conn.commit()
    yield TestClient(api.create_app(cfg)), conn, cfg
    conn.close()


def create(client, **values):
    return client.post('/api/video-markers', json={'file_id': 1, 'time_s': 12.125, **values})


def test_marker_crud_and_etag(marker_library):
    client, conn, cfg = marker_library
    etag = client.get('/api/library').headers['etag']
    response = create(client)
    assert response.status_code == 201
    marker = response.json()
    assert marker['time_s'] == 12.125 and marker['note'] == ''
    assert marker['file_id'] == 1 and marker['created_at'] and marker['updated_at']
    path = f"/api/video-markers/{marker['id']}"
    # A new app/connection sees the committed marker.
    fresh = TestClient(api.create_app(cfg))
    assert fresh.get('/api/video-markers?file_id=1').json()['markers'] == [marker]
    assert client.patch(path, json={'note': '<b>plain text</b>'}).json()['time_s'] == 12.125
    assert client.patch(path, json={'time_s': 0}).json()['note'] == '<b>plain text</b>'
    assert client.patch(path, json={'note': ''}).json()['note'] == ''
    assert client.patch(path, json={'time_s': 60}).status_code == 200
    assert client.patch(path, json={'time_s': 60.1}).status_code == 422
    assert client.get('/api/library', headers={'If-None-Match': etag}).status_code == 304
    assert client.delete(path).status_code == 204
    assert client.get('/api/video-markers').json() == {'markers': []}
    assert client.patch(path, json={'note': 'gone'}).status_code == 404
    assert client.delete(path).status_code == 404


@pytest.mark.parametrize('value', [-1, 60.001, 'NaN', 'Infinity', '-Infinity', None, 'abc'])
def test_bad_timestamps_rejected(marker_library, value):
    client, _, _ = marker_library
    assert create(client, time_s=value).status_code == 422
    marker = create(client).json()
    assert client.patch(f"/api/video-markers/{marker['id']}", json={'time_s': value}).status_code == 422


def test_notes_and_same_time_markers(marker_library):
    client, _, _ = marker_library
    a = create(client, note='first').json()
    b = create(client, note='second').json()
    assert a['id'] != b['id']
    assert [m['id'] for m in client.get('/api/video-markers').json()['markers']] == [a['id'], b['id']]
    assert create(client, note=None).status_code == 422
    assert create(client, note='a' * 10001).status_code == 422


@pytest.mark.parametrize('change', ["media_type='photo'", "status='quarantined'", 'lat=NULL', 'lon=NULL'])
def test_ineligible_files_and_existing_markers_are_hidden(marker_library, change):
    client, conn, _ = marker_library
    marker = create(client).json()
    conn.execute(f'UPDATE files SET {change} WHERE id=1')
    conn.commit()
    assert client.get('/api/video-markers').json() == {'markers': []}
    assert client.get('/api/video-markers?file_id=1').status_code == 404
    assert create(client).status_code == 404
    assert client.patch(f"/api/video-markers/{marker['id']}", json={'note': 'x'}).status_code == 404
    assert client.delete(f"/api/video-markers/{marker['id']}").status_code == 404
    assert conn.execute('SELECT COUNT(*) FROM video_markers').fetchone()[0] == 1


def test_move_duplicate_and_reimport_identity(marker_library):
    client, conn, _ = marker_library
    marker = create(client).json()
    conn.execute("UPDATE files SET dest_path='moved/clip.mp4' WHERE id=1")
    conn.execute("INSERT INTO files(id,dest_path,filename,media_type,status,sha256,lat,lon,duration_s) "
                 "VALUES (2,'copy.mp4','copy.mp4','video','organized','video-hash',40,-105,60)")
    conn.commit()
    assert len(client.get('/api/video-markers').json()['markers']) == 1
    assert client.get('/api/video-markers?file_id=2').json()['markers'][0]['id'] == marker['id']
    conn.execute('DELETE FROM files')
    conn.commit()
    assert client.get('/api/video-markers').json()['markers'] == []
    conn.execute("INSERT INTO files(id,dest_path,filename,media_type,status,sha256,lat,lon,duration_s) "
                 "VALUES (3,'reimport.mp4','reimport.mp4','video','organized','video-hash',40,-105,60)")
    conn.commit()
    restored = client.get('/api/video-markers').json()['markers'][0]
    assert restored['id'] == marker['id'] and restored['file_id'] == 3


def test_unknown_duration_and_hyperlapse(marker_library):
    client, conn, _ = marker_library
    conn.execute("UPDATE files SET duration_s=NULL,capture_kind='hyperlapse'")
    conn.commit()
    assert create(client, time_s=999).status_code == 201
    assert create(client, file_id=999).status_code == 404


def test_admin_gate(marker_library):
    _, _, cfg = marker_library
    cfg = replace(cfg, admin_password_hash=auth.hash_password('markers-test', iterations=1))
    client = TestClient(api.create_app(cfg))
    assert client.get('/api/video-markers').status_code == 200
    assert create(client).status_code == 401
    assert client.patch('/api/video-markers/1', json={'note': 'x'}).status_code == 401
    assert client.delete('/api/video-markers/1').status_code == 401
    token = client.post('/api/login', json={'password': 'markers-test'}).json()['token']
    client.headers['Authorization'] = f'Bearer {token}'
    marker = create(client).json()
    assert client.patch(f"/api/video-markers/{marker['id']}", json={'note': 'x'}).status_code == 200
    assert client.delete(f"/api/video-markers/{marker['id']}").status_code == 204


def test_v6_migration_is_lossless_and_idempotent(tmp_path):
    conn = db.connect(tmp_path / 'v6.db')
    try:
        db.init_index_schema(conn)
        conn.execute('DROP TABLE video_markers')
        conn.execute('UPDATE schema_version SET version=6')
        conn.execute("INSERT INTO favorites(sha256) VALUES ('keep')")
        conn.commit()
        db.init_index_schema(conn)
        conn.execute("INSERT INTO video_markers(sha256,time_s,note) VALUES ('keep',1.25,'note')")
        conn.commit()
        db.init_index_schema(conn)
        assert conn.execute('SELECT version FROM schema_version').fetchone()[0] == 7
        assert conn.execute('SELECT COUNT(*) FROM schema_version').fetchone()[0] == 1
        assert conn.execute('SELECT sha256 FROM favorites').fetchone()[0] == 'keep'
        assert tuple(conn.execute('SELECT time_s,note FROM video_markers').fetchone()) == (1.25, 'note')
    finally:
        conn.close()
