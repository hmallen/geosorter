"""Admin-auth core: password hashing + an in-memory session-token store.

The map UI's media-management actions (organize/undo/rescan/retag/assign/stitch)
are gated behind a single shared admin password. This module holds the two pure
pieces the HTTP layer wires together:

* :func:`hash_password` / :func:`verify_password` — a dependency-free PBKDF2-SHA256
  hash in the self-describing ``pbkdf2_sha256$<iters>$<salt_hex>$<hash_hex>``
  format (the algorithm, iteration count, and salt travel with the digest, so a
  future cost bump stays verifiable against old hashes). The hash lives in
  ``geosorter.toml`` (``admin_password_hash``), set via ``geosorter set-admin-password``.
* :class:`TokenStore` — a thread-safe set of live bearer tokens. ``/api/login``
  issues one, ``require_admin`` checks membership, ``/api/logout`` revokes it. The
  store is in-memory only (tokens reset on server restart — the operator re-logs-in),
  which is the right trade-off for a single-owner LAN tool.

No persistence, no external dependency: ``hashlib``/``hmac``/``secrets`` only.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading

_ALGORITHM = "pbkdf2_sha256"
_DEFAULT_ITERATIONS = 200_000
_SALT_BYTES = 16
# Upper bound for a stored hash's iteration count. Rejects a hand-corrupted hash whose
# count would overflow pbkdf2_hmac's C long (raising OverflowError) or be so large the
# login hash hangs — both turned into a clean auth failure rather than a 500/DoS.
_MAX_ITERATIONS = 100_000_000


def hash_password(password: str, *, iterations: int = _DEFAULT_ITERATIONS) -> str:
    """Hash ``password`` into ``pbkdf2_sha256$<iters>$<salt_hex>$<hash_hex>``.

    A fresh random salt is generated per call, so two hashes of the same password
    differ. Pass a custom ``iterations`` only in tests that need a cheaper hash.
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGORITHM}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of ``password`` against a :func:`hash_password` string.

    Returns ``False`` (never raises) on any malformed/unexpected ``stored`` value,
    so a corrupt config key simply fails to authenticate rather than crashing the
    login route.
    """
    try:
        algorithm, iters_s, salt_hex, hash_hex = stored.split("$")
        if algorithm != _ALGORITHM:
            return False
        iterations = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        # Guard the degenerate parses that split/int/fromhex accept but pbkdf2_hmac
        # rejects (iterations out of [1, _MAX_ITERATIONS], empty salt/digest), so a
        # hand-corrupted hash fails authentication cleanly instead of escaping this try
        # and 500-ing /api/login. OverflowError is also caught belt-and-suspenders.
        if iterations < 1 or iterations > _MAX_ITERATIONS or not salt or not expected:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    except (ValueError, AttributeError, OverflowError):
        return False
    return hmac.compare_digest(actual, expected)


class TokenStore:
    """Thread-safe set of live admin bearer tokens (in-memory, no expiry).

    The FastAPI app and the background job threads share one process, so the set is
    guarded by a lock. Tokens are opaque, URL-safe, and high-entropy
    (:func:`secrets.token_urlsafe`).
    """

    def __init__(self) -> None:
        self._tokens: set[str] = set()
        self._lock = threading.Lock()

    def issue(self) -> str:
        """Generate, store, and return a new bearer token."""
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._tokens.add(token)
        return token

    def valid(self, token: str | None) -> bool:
        """Whether ``token`` is a currently-live issued token (``False`` for ``None``)."""
        if not token:
            return False
        with self._lock:
            return token in self._tokens

    def revoke(self, token: str | None) -> None:
        """Invalidate ``token`` if present; a no-op for unknown/``None`` tokens."""
        if not token:
            return
        with self._lock:
            self._tokens.discard(token)
