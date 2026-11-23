"""SimHash near-duplicate detection.

A 64-bit fingerprint of word shingles: similar texts produce fingerprints a few bits apart.
Hamming distance of 3 or less means "basically the same post". Stored per business (to avoid
repeating ourselves) and globally as hashes only (so one customer can't spam the same text across
many accounts and get the platform's apps banned for everyone).
"""

from __future__ import annotations

import hashlib
import re

_TOKEN = re.compile(r"[a-z0-9']+")
NEAR_DUPLICATE_BITS = 3


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def simhash(text: str, *, shingle: int = 3) -> int:
    tokens = _tokens(text)
    if not tokens:
        return 0
    grams = [" ".join(tokens[i : i + shingle]) for i in range(max(len(tokens) - shingle + 1, 1))]
    vector = [0] * 64
    for gram in grams:
        h = int.from_bytes(hashlib.blake2b(gram.encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            vector[bit] += 1 if (h >> bit) & 1 else -1
    value = 0
    for bit in range(64):
        if vector[bit] > 0:
            value |= 1 << bit
    return value


def to_signed(value: int) -> int:
    """PostgreSQL BIGINT is signed; store the 64-bit fingerprint as its two's-complement value."""
    return value - (1 << 64) if value >= 1 << 63 else value


def to_unsigned(value: int) -> int:
    return value + (1 << 64) if value < 0 else value


def distance(a: int, b: int) -> int:
    return (to_unsigned(a) ^ to_unsigned(b)).bit_count()


def band(value: int) -> int:
    """Top 16 bits: a cheap index to find candidates before computing exact distances."""
    return to_unsigned(value) >> 48
