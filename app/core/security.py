"""
app/core/security.py
────────────────────
Password hashing (bcrypt), JWT access tokens, refresh-token helpers and a
small in-memory login throttle.

Access tokens are stateless short-lived JWTs; refresh sessions live in the
`sessions` table (stored as SHA-256 digests) so logout actually revokes them.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from jwt import InvalidTokenError

from app.core.config import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    JWT_ALGORITHM,
    JWT_SECRET,
    LOGIN_MAX_ATTEMPTS,
    LOGIN_WINDOW_SECONDS,
)

# bcrypt silently truncates input at 72 bytes; reject instead of weakening.
MAX_PASSWORD_BYTES = 72


# ── Passwords ──────────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


# Verified against when the identifiant is unknown, so response time does not
# reveal whether an account exists.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def verify_password_constant_time(plain: str, hashed: str | None) -> bool:
    return verify_password(plain, hashed if hashed else _DUMMY_HASH) and hashed is not None


# ── JWT access tokens ──────────────────────────────────────────────────────────

def create_access_token(user_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "typ": "access",
        "iat": now,
        "exp": now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> str | None:
    """Return the user_id (`sub`) or None if invalid/expired."""
    try:
        payload = jwt.decode(
            token, JWT_SECRET, algorithms=[JWT_ALGORITHM],
            options={"require": ["exp", "iat", "sub"]},
        )
    except InvalidTokenError:
        return None
    if payload.get("typ") != "access":
        return None
    return payload.get("sub")


# ── Refresh tokens ─────────────────────────────────────────────────────────────

def new_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    """Digest persisted in the DB, so a database leak does not leak live sessions."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ── Login throttle ─────────────────────────────────────────────────────────────

class LoginThrottle:
    """Sliding-window failed-attempt counter (per process, in memory).

    Good enough for a single-instance deployment; use a shared store (Redis)
    or the reverse proxy's rate limiting when running several replicas.
    """

    def __init__(self, max_attempts: int, window_seconds: int) -> None:
        self._max = max_attempts
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        hits = self._hits[key]
        while hits and now - hits[0] > self._window:
            hits.popleft()
        if not hits:
            self._hits.pop(key, None)
        return hits

    def is_blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._prune(key, time.monotonic())) >= self._max

    def record_failure(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._prune(key, now)
            self._hits[key].append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


login_throttle = LoginThrottle(LOGIN_MAX_ATTEMPTS, LOGIN_WINDOW_SECONDS)
