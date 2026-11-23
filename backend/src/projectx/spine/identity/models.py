from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Integer, LargeBinary, String, func, true
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TimestampMixin


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(CITEXT, unique=True)
    name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    email_verified_at: Mapped[datetime | None]
    last_login_at: Mapped[datetime | None]
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())
    is_staff: Mapped[bool] = mapped_column(default=False, server_default="false")
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    locked_until: Mapped[datetime | None]


class UserSession(IdMixin, Base):
    """A signed-in browser. Only a SHA-256 of the cookie token is stored."""

    __tablename__ = "user_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    user_agent: Mapped[str | None] = mapped_column(String(300))
    ip_hash: Mapped[str | None] = mapped_column(String(64))


class EmailToken(IdMixin, Base):
    """Single-use links: email verification, password reset, invitations."""

    __tablename__ = "email_tokens"

    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(String(30))
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]


class AuthThrottle(Base):
    """Fixed-window counters for sign-in and other abuse-prone endpoints."""

    __tablename__ = "auth_throttle"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    window_start: Mapped[datetime]
    count: Mapped[int] = mapped_column(Integer, default=0)
