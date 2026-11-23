"""Passwords, tokens, CSRF and throttling."""

from __future__ import annotations

import hashlib
import hmac
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.errors import RateLimited

# Argon2id with the library's current recommended parameters (RFC 9106 low-memory profile).
_hasher = PasswordHasher()

# A real hash of a random password, used to keep sign-in timing constant for unknown emails.
_DUMMY_HASH = _hasher.hash("not-a-real-password-just-for-timing")

MIN_PASSWORD_LENGTH = 10


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> tuple[bool, str | None]:
    """Returns (ok, new_hash). new_hash is set when parameters changed and the hash should be upgraded."""
    try:
        _hasher.verify(password_hash or _DUMMY_HASH, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False, None
    if password_hash is None:
        return False, None
    if _hasher.check_needs_rehash(password_hash):
        return True, _hasher.hash(password)
    return True, None


def password_problem(password: str, *, email: str | None = None, name: str | None = None) -> str | None:
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > 256:
        return "That password is too long."
    lowered = password.lower()
    if email and email.split("@")[0].lower() in lowered and len(email.split("@")[0]) >= 4:
        return "Don't use your email in your password."
    if len(set(password)) < 4:
        return "Use a less repetitive password."
    if lowered in _COMMON:
        return "That password is too common."
    return None


_COMMON = frozenset(
    {
        "password123",
        "1234567890",
        "qwertyuiop",
        "iloveyou12",
        "password1234",
        "letmein1234",
        "welcome1234",
        "admin12345",
        "abc1234567",
        "1q2w3e4r5t",
    }
)


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def hash_ip(ip: str | None, secret: str) -> str | None:
    if not ip:
        return None
    return hmac.new(secret.encode(), ip.encode(), hashlib.sha256).hexdigest()[:32]


def csrf_token_for(session_token_hash: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), b"csrf:" + session_token_hash, hashlib.sha256).hexdigest()


def csrf_valid(session_token_hash: bytes, presented: str | None, secret: str) -> bool:
    if not presented:
        return False
    return hmac.compare_digest(csrf_token_for(session_token_hash, secret), presented)


async def throttle(session: AsyncSession, key: str, *, limit: int, window: timedelta) -> None:
    """Fixed-window counter in PostgreSQL. Raises RateLimited when `limit` is exceeded."""
    seconds = int(window.total_seconds())
    row = (
        await session.execute(
            text(
                """
            INSERT INTO auth_throttle (key, window_start, count) VALUES (:key, now(), 1)
            ON CONFLICT (key) DO UPDATE SET
              count = CASE WHEN auth_throttle.window_start < now() - make_interval(secs => :secs)
                           THEN 1 ELSE auth_throttle.count + 1 END,
              window_start = CASE WHEN auth_throttle.window_start < now() - make_interval(secs => :secs)
                           THEN now() ELSE auth_throttle.window_start END
            RETURNING count AS hits,
                      EXTRACT(EPOCH FROM (window_start + make_interval(secs => :secs) - now()))::int AS remaining
            """
            ),
            {"key": key[:200], "secs": seconds},
        )
    ).one()
    if row.hits > limit:
        raise RateLimited("Too many attempts. Please wait and try again.", retry_after=max(int(row.remaining), 1))
