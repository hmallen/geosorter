from __future__ import annotations

import io
import zipfile

import httpx
import pytest

from geosorter import geonames_loader


@pytest.mark.parametrize("response_kind", ["ignored", "wrong-offset", "unsatisfiable", "valid"])
def test_download_resume_requires_valid_range(tmp_path, monkeypatch, response_kind):
    target = tmp_path / "data.txt"
    target.write_bytes(b"ab")
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1 and response_kind == "wrong-offset":
            return httpx.Response(206, headers={"Content-Range": "bytes 0-3/6"}, content=b"cdef")
        if len(calls) == 1 and response_kind == "unsatisfiable":
            return httpx.Response(416)
        if len(calls) == 1 and response_kind == "valid":
            return httpx.Response(206, headers={"Content-Range": "bytes 2-5/6"}, content=b"cdef")
        return httpx.Response(200, content=b"abcdef")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(geonames_loader.httpx, "stream", client.stream)
        geonames_loader._download_file("https://example.invalid/data.txt", target)
        assert target.read_bytes() == b"abcdef"
        count = len(calls)
        geonames_loader._download_file("https://example.invalid/data.txt", target)
        assert len(calls) == count  # verified completed download survives retry
    if response_kind in ("wrong-offset", "unsatisfiable"):
        assert len(calls) == 2
        assert "Range" not in calls[1].headers


def test_incomplete_response_is_not_marked_complete(tmp_path, monkeypatch):
    target = tmp_path / "data.txt"
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, headers={"Content-Length": "6"}, content=b"ab"))) as client:
        monkeypatch.setattr(geonames_loader.httpx, "stream", client.stream)
        with pytest.raises(ValueError, match="before all bytes"):
            geonames_loader._download_file("https://example.invalid/data.txt", target)
    assert not target.with_suffix(".txt.complete.json").exists()
    assert target.read_bytes() == b"ab"


def test_corrupt_archive_is_redownloaded(tmp_path, monkeypatch):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("cities500.txt", "city data")
    calls = []
    def handler(request):
        if request.url.path.endswith("cities500.zip"):
            calls.append(request)
            return httpx.Response(200, content=b"corrupt" if len(calls) == 1 else stream.getvalue())
        return httpx.Response(200, content=b"lookup data")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(geonames_loader.httpx, "stream", client.stream)
        geonames_loader.download(tmp_path, base_url="https://example.invalid/", min_free_mb=0)
    assert len(calls) == 2
    assert (tmp_path / "cities500.txt").read_text() == "city data"


def test_modified_completed_download_is_not_reused(tmp_path, monkeypatch):
    target = tmp_path / "data.txt"
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=b"correct")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(geonames_loader.httpx, "stream", client.stream)
        geonames_loader._download_file("https://example.invalid/data.txt", target)
        target.write_bytes(b"corrupt")
        geonames_loader._download_file("https://example.invalid/data.txt", target)
    assert len(calls) == 2
    assert target.read_bytes() == b"correct"


def test_index_staging_space_check_preserves_existing_database(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from geosorter import bootstrap
    active = tmp_path / "places.db"
    active.write_bytes(b"previous working database")
    source = tmp_path / "source"
    source.mkdir()
    (source / "cities500.txt").write_text("source data")
    monkeypatch.setattr(bootstrap.shutil, "disk_usage", lambda path: SimpleNamespace(free=1))
    with pytest.raises(ValueError, match="free space"):
        bootstrap.run(SimpleNamespace(geonames_db_path=active), source=source)
    assert active.read_bytes() == b"previous working database"
