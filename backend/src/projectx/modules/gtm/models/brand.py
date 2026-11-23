from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class BrandBook(IdMixin, TimestampMixin, TenantMixin, Base):
    """Step 0 of the GTM loop: voice, offer, audience, visual rules, do and don't, proof points.
    Every post, ad and reply is written from the approved version. Versions are immutable once
    approved; editing creates a new draft."""

    __tablename__ = "gtm_brand_books"
    __table_args__ = (Index("uq_gtm_brand_books_version", "business_id", "version", unique=True),)

    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | approved | archived
    summary: Mapped[str] = mapped_column(Text, default="")
    voice: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    offer: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    audience_summary: Mapped[str] = mapped_column(Text, default="")
    positioning: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    visual: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    do_list: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    dont_list: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    banned_words: Mapped[list[str]] = mapped_column(ARRAY(String(80)), default=list, server_default="{}")
    proof_points: Mapped[list[Any]] = mapped_column(default=list, server_default="[]")
    hashtags: Mapped[list[str]] = mapped_column(ARRAY(String(80)), default=list, server_default="{}")
    sources: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    created_by: Mapped[str] = mapped_column(String(100), default="user")
    approved_by: Mapped[uuid.UUID | None]
    approved_at: Mapped[datetime | None]


class Audience(IdMixin, TimestampMixin, TenantMixin, Base):
    """A customer profile (who exactly to target). The primary one steers platform choice."""

    __tablename__ = "gtm_audiences"

    name: Mapped[str] = mapped_column(String(120))
    is_primary: Mapped[bool] = mapped_column(default=False)
    description: Mapped[str] = mapped_column(Text, default="")
    segment: Mapped[str] = mapped_column(String(10), default="b2c")  # b2c | b2b
    demographics: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    firmographics: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    pains: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    desires: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    objections: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    buying_triggers: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    # Where this audience spends time: {"instagram": 0.8, "linkedin": 0.2, ...}
    platform_affinity: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    locations: Mapped[list[str]] = mapped_column(ARRAY(String(80)), default=list, server_default="{}")
