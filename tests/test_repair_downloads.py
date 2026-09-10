"""The optional-tool downloader must work without populated Windows roots."""
import io
import ssl
import urllib.error

import pytest

from geosorter import repair


@pytest.fixture
def empty_native_roots(monkeypatch):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    assert not context.get_ca_certs()
    monkeypatch.setattr(repair.ssl, "create_default_context", lambda: context)
    return context


def test_bundled_roots_supplement_default_context(empty_native_roots):
    context = repair._download_context()
    assert context is empty_native_roots  # retains the context's native/custom roots
    assert context.get_ca_certs()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname


@pytest.mark.parametrize("kind", ["metadata", "archive"])
def test_both_downloads_use_verified_bundled_roots(tmp_path, monkeypatch, empty_native_roots, kind):
    def open_url(request, *, timeout, context):
        assert context.get_ca_certs()
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname
        payload = b'{"tag_name":"test"}' if kind == "metadata" else b"archive bytes"
        response = io.BytesIO(payload)
        response.headers = {"Content-Length": str(len(payload))}
        return response
    monkeypatch.setattr(repair.urllib.request, "urlopen", open_url)
    if kind == "metadata":
        assert repair._fetch_json(repair.UNTRUNC_RELEASE_API) == {"tag_name": "test"}
    else:
        progress = []
        dest = tmp_path / "download.zip"
        repair._fetch_to_file("https://example.invalid/test.zip", dest, lambda done, total: progress.append((done, total)))
        assert dest.read_bytes() == b"archive bytes"
        assert progress[-1] == (13, 13)


@pytest.mark.parametrize("kind", ["metadata", "archive"])
def test_certificate_failure_is_not_retried_without_verification(tmp_path, monkeypatch, kind):
    calls = []
    def reject(request, *, timeout, context):
        calls.append(context)
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname
        raise urllib.error.URLError(ssl.SSLCertVerificationError(1, "untrusted certificate"))
    monkeypatch.setattr(repair.urllib.request, "urlopen", reject)
    with pytest.raises(urllib.error.URLError):
        if kind == "metadata":
            repair._fetch_json(repair.UNTRUNC_RELEASE_API)
        else:
            repair._fetch_to_file("https://example.invalid/test.zip", tmp_path / "download.zip", None)
    assert len(calls) == 1
    assert not (tmp_path / "download.zip").exists()
