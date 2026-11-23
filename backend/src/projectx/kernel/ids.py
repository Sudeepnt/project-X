"""Identifiers.

Primary keys are UUIDv7 (RFC 9562): time-ordered, so B-tree indexes stay compact and ids sort
by creation time. We generate them in Python so a row's id is known before it is inserted
(ledger events reference it in the same transaction). PostgreSQL 18's ``uuidv7()`` is the
server-side default for rows created outside the application.
"""

from __future__ import annotations

import os
import secrets
import string
import time
import uuid

_RAND_A_MASK = (1 << 12) - 1
_RAND_B_MASK = (1 << 62) - 1
_TS_MASK = (1 << 48) - 1
_BASE62 = string.digits + string.ascii_letters


def uuid7() -> uuid.UUID:
    """Return a new UUIDv7: 48-bit unix-ms timestamp, version 7, RFC 4122 variant, 74 random bits."""
    ts_ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    rand_a = (rand >> 62) & _RAND_A_MASK
    rand_b = rand & _RAND_B_MASK
    value = (ts_ms & _TS_MASK) << 80 | 0x7 << 76 | rand_a << 64 | 0b10 << 62 | rand_b
    return uuid.UUID(int=value)


def short_code(length: int = 8) -> str:
    """A random base62 code, e.g. for tracked-link slugs. 8 chars ~ 47.6 bits of entropy."""
    return "".join(secrets.choice(_BASE62) for _ in range(length))


def token_urlsafe(nbytes: int = 32) -> str:
    """A random, URL-safe secret token (session tokens, verification links)."""
    return secrets.token_urlsafe(nbytes)
