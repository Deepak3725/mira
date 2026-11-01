"""Minimal local-demo authentication (standard library only).

- Passwords: PBKDF2-HMAC-SHA256, 200k iterations, per-user random salt.
- Sessions: stateless HMAC-signed cookies (user_id:expiry:signature).
- Rate limiting: in-memory sliding window per key.

Honest limits: fine for a local hackathon demo with a small audience; this
is not a hardened internet-scale login system (see README security notes).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Any

from .config import settings

SESSION_COOKIE = "mira_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600
_PBKDF2_ITERATIONS = 200_000
#: Passwords longer than this are rejected before hashing (PBKDF2 over
#: unbounded input is a CPU-DoS vector). 128 chars is ample for passphrases.
MAX_PASSWORD_LENGTH = 128

_rate_buckets: dict[str, list[float]] = {}
#: Upper bound on tracked limiter keys. Stale keys are evicted past this
#: point so a flood of distinct keys cannot grow memory without limit.
RATE_BUCKET_CAP = 10000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = (stored or "").split("$")
        if algo != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def _sign(payload: str) -> str:
    return hmac.new(
        settings.session_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def create_session(user_id: int, ttl: int = SESSION_TTL_SECONDS) -> str:
    payload = f"{user_id}:{int(time.time()) + ttl}"
    return f"{payload}:{_sign(payload)}"


def parse_session(value: str | None) -> int | None:
    try:
        user_id, expiry, sig = (value or "").split(":")
        if not hmac.compare_digest(_sign(f"{user_id}:{expiry}"), sig):
            return None
        if int(expiry) < time.time():
            return None
        return int(user_id)
    except (ValueError, TypeError):
        return None


def current_user_id(request: Any) -> int | None:
    value = request.cookies.get(SESSION_COOKIE)
    user_id = parse_session(value)
    if user_id is None or not value:
        return None
    try:
        from .db import get_connection

        conn = get_connection()
        try:
            if session_revoked(conn, value):
                return None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - fail open only for the check itself
        return user_id
    return user_id


def csrf_token(user_id: int | None) -> str:
    """Per-user form token (HMAC of the session secret). No server state."""
    return _sign(f"csrf:{user_id or 0}")


def valid_csrf(token: str | None, user_id: int | None) -> bool:
    return bool(token) and hmac.compare_digest(token, csrf_token(user_id))


def require_csrf(token: str | None, user_id: int | None) -> None:
    """Raise 403 unless the form carries this user's CSRF token."""
    from fastapi import HTTPException

    if not valid_csrf(token, user_id):
        raise HTTPException(status_code=403, detail="Invalid form token.")


def session_csrf_token(user_id: int | None, session_value: str) -> str:
    """CSRF token bound to the current session cookie.

    Rotates on every login (new session value, new token), so a leaked
    token dies with the session. Anonymous flows (login/register)
    keep the unbound csrf_token() above.
    """
    return _sign(f"csrf:{user_id or 0}:{session_value or ''}")


def valid_session_csrf(token: str | None, user_id: int | None,
                       session_value: str) -> bool:
    return bool(token) and hmac.compare_digest(
        token, session_csrf_token(user_id, session_value))


def csrf_for_request(request: Any, user_id: int | None) -> str:
    """Issue the session-bound token for this request's cookie."""
    return session_csrf_token(
        user_id, request.cookies.get(SESSION_COOKIE, ""))


def require_session_csrf(request: Any, token: str | None,
                         user_id: int | None) -> None:
    """Raise 403 unless the token matches the request's own session."""
    from fastapi import HTTPException

    if not valid_session_csrf(
            token, user_id, request.cookies.get(SESSION_COOKIE, "")):
        raise HTTPException(status_code=403, detail="Invalid form token.")


def session_hash(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def revoke_session(conn, session_value: str) -> None:
    """Record a logout so the cookie stops working (idempotent)."""
    import datetime as _dt

    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    digest = session_hash(session_value)
    exists = conn.execute("SELECT 1 AS one FROM revoked_sessions"
                          " WHERE token_hash = ?", (digest,)).fetchone()
    if exists is None:
        conn.execute("INSERT INTO revoked_sessions (token_hash, revoked_at)"
                     " VALUES (?, ?)", (digest, stamp))
        conn.commit()


def session_revoked(conn, session_value: str) -> bool:
    """True when the cookie was revoked. Missing table = nothing revoked."""
    try:
        row = conn.execute("SELECT 1 AS one FROM revoked_sessions"
                           " WHERE token_hash = ?",
                           (session_hash(session_value),)).fetchone()
    except Exception:  # noqa: BLE001 - pre-v12 databases lack the table
        return False
    return row is not None


def _evict_rate_buckets(now: float) -> None:
    """Drop expired keys; if still over cap, drop the oldest half."""
    for known in [k for k, stamps in _rate_buckets.items()
                  if not any(t > now - 3600 for t in stamps)]:
        del _rate_buckets[known]
    if len(_rate_buckets) > RATE_BUCKET_CAP:
        for known in list(_rate_buckets)[:len(_rate_buckets) // 2]:
            del _rate_buckets[known]


def check_rate_limit(key: str, limit: int, window_seconds: int) -> bool:
    """Sliding-window limiter. Returns True if the call is allowed."""
    now = time.monotonic()
    bucket = [t for t in _rate_buckets.get(key, []) if now - t < window_seconds]
    if len(bucket) >= limit:
        _rate_buckets[key] = bucket
        return False
    bucket.append(now)
    _rate_buckets[key] = bucket
    if len(_rate_buckets) > RATE_BUCKET_CAP:
        _evict_rate_buckets(now)
    return True


def reset_rate_limits() -> None:
    _rate_buckets.clear()
