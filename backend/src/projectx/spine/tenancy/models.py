from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TimestampMixin

ROLES = ("owner", "admin", "editor", "viewer")
ROLE_RANK = {role: rank for rank, role in enumerate(reversed(ROLES))}  # viewer=0 ... owner=3

BUSINESS_MODELS = ("media", "service", "software", "ecommerce", "creator", "other")


class Business(IdMixin, TimestampMixin, Base):
    """A tenant. Everything a business owns hangs off this id and lives in its sealed room."""

    __tablename__ = "businesses"

    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(60), unique=True)
    website: Mapped[str | None] = mapped_column(String(300))
    industry: Mapped[str | None] = mapped_column(String(80))
    business_model: Mapped[str] = mapped_column(String(20), default="other", server_default="other")
    description: Mapped[str | None] = mapped_column(String(1000))
    country: Mapped[str | None] = mapped_column(String(2))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", server_default="UTC")
    currency: Mapped[str] = mapped_column(String(3), default="USD", server_default="USD")
    status: Mapped[str] = mapped_column(String(20), default="active", server_default="active")
    plan: Mapped[str] = mapped_column(String(30), default="free", server_default="free")
    practice_mode: Mapped[bool] = mapped_column(default=False, server_default="false")
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    onboarding: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")


class Membership(TimestampMixin, Base):
    __tablename__ = "memberships"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    role: Mapped[str] = mapped_column(String(20))
