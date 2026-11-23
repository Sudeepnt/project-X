"""Opaque keyset cursors. Offset pagination degrades on large tables; keysets do not."""

from __future__ import annotations

import base64
import uuid
from datetime import datetime

import orjson
from pydantic import BaseModel

from projectx.kernel.errors import ValidationFailed

MAX_LIMIT = 200
DEFAULT_LIMIT = 50


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(limit, MAX_LIMIT))


def encode_cursor(ts: datetime, row_id: uuid.UUID) -> str:
    raw = orjson.dumps([ts.isoformat(), str(row_id)])
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        ts_raw, id_raw = orjson.loads(base64.urlsafe_b64decode(padded))
        return datetime.fromisoformat(ts_raw), uuid.UUID(id_raw)
    except (ValueError, TypeError, orjson.JSONDecodeError) as exc:
        raise ValidationFailed("Invalid cursor") from exc
