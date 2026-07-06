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


# --- LoginThrottle ---------------------------------------------------------- #


def _throttle(**kw):
    """A LoginThrottle on a controllable fake clock; returns (throttle, tick)."""
    now = {"t": 1000.0}
    th = auth.LoginThrottle(clock=lambda: now["t"], **kw)

    def tick(seconds: float) -> None:
        now["t"] += seconds

    return th, tick


def test_throttle_allows_below_failure_cap():
    th, _ = _throttle(max_failures=3, lockout_s=30.0)
    th.record_failure("ip")
    th.record_failure("ip")
    assert th.retry_after("ip") == 0.0  # 2 < 3: still allowed


def test_throttle_locks_out_at_cap_and_expires():
    th, tick = _throttle(max_failures=3, lockout_s=30.0)
    for _ in range(3):
        th.record_failure("ip")
    assert th.retry_after("ip") > 0.0
    tick(31.0)
    assert th.retry_after("ip") == 0.0  # lockout expired
    # One more failure after expiry re-arms a FULL lockout immediately.
    th.record_failure("ip")
    assert th.retry_after("ip") > 0.0


def test_throttle_success_clears_slate():
    th, _ = _throttle(max_failures=3, lockout_s=30.0)
    for _ in range(3):
        th.record_failure("ip")
    th.record_success("ip")
    assert th.retry_after("ip") == 0.0


def test_throttle_is_per_key():
    th, _ = _throttle(max_failures=3, lockout_s=30.0)
    for _ in range(3):
        th.record_failure("attacker")
    assert th.retry_after("attacker") > 0.0
    assert th.retry_after("owner") == 0.0


def test_throttle_prunes_expired_entries_at_capacity():
    th, tick = _throttle(max_failures=1, lockout_s=10.0)
    for i in range(th._MAX_ENTRIES):
        th.record_failure(f"ip{i}")
    tick(100.0)  # everything long expired
    th.record_failure("fresh")  # triggers the prune at capacity
    assert len(th._state) < th._MAX_ENTRIES
