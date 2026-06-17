"""Tests for the admin-auth core (password hashing + in-memory token store)."""

from __future__ import annotations

from geosorter import auth


def test_hash_verify_roundtrip():
    stored = auth.hash_password("s3cret")
    assert stored.startswith("pbkdf2_sha256$")
    assert auth.verify_password("s3cret", stored) is True


def test_hash_is_salted_unique():
    # Two hashes of the same password differ (random per-hash salt).
    assert auth.hash_password("s3cret") != auth.hash_password("s3cret")


def test_verify_wrong_password_false():
    stored = auth.hash_password("s3cret")
    assert auth.verify_password("wrong", stored) is False


def test_verify_malformed_hash_false():
    # Any non-conforming stored value returns False rather than raising. The
    # iterations<1 cases ($0$/$-1$) parse cleanly via int() but make pbkdf2_hmac
    # raise — verify_password must still return False, never propagate.
    for bad in (
        "",
        "not-a-hash",
        "pbkdf2_sha256$abc",
        "a$b$c$d",
        "pbkdf2_sha256$x$y$z",
        "pbkdf2_sha256$0$00$ff",
        "pbkdf2_sha256$-1$00$ff",
        "pbkdf2_sha256$1$$ff",
        # Oversized iteration count: int() accepts it but pbkdf2_hmac would raise
        # OverflowError (and a merely-huge valid count would hang) — both must -> False.
        "pbkdf2_sha256$" + "9" * 20 + "$00$ff",
        "pbkdf2_sha256$999999999$00$ff",
    ):
        assert auth.verify_password("s3cret", bad) is False


def test_tokenstore_issue_valid_revoke():
    store = auth.TokenStore()
    tok = store.issue()
    assert isinstance(tok, str) and tok
    assert store.valid(tok) is True
    assert store.valid("nope") is False
    store.revoke(tok)
    assert store.valid(tok) is False
    # Revoking an unknown / already-revoked token is a no-op (never raises).
    store.revoke(tok)
    store.revoke("never-issued")


def test_tokenstore_issues_unique_tokens():
    store = auth.TokenStore()
    tokens = {store.issue() for _ in range(50)}
    assert len(tokens) == 50


def test_valid_none_token_false():
    store = auth.TokenStore()
    assert store.valid(None) is False
