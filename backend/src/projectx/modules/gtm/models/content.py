from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TenantMixin, TimestampMixin

PILLARS = ("educate", "entertain", "inspire", "behind_the_scenes", "proof", "offer")
CONTENT_STATUSES = (
    "idea",
    "draft",
    "needs_review",
    "approved",
    "scheduled",
    "published",
    "rejected",
    "archived",
    "failed",
)


class ContentTheme(IdMixin, TimestampMixin, TenantMixin, Base):
    """Four to six themes per business, each tied to a pillar. Weights adapt to what performs."""

    __tablename__ = "gtm_content_themes"

    name: Mapped[str] = mapped_column(String(120))
    pillar: Mapped[str] = mapped_column(String(30))
    description: Mapped[str] = mapped_column(Text, default="")
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    active: Mapped[bool] = mapped_column(default=True)


class ContentItem(IdMixin, TimestampMixin, TenantMixin, Base):
    """One core idea. It becomes a native version per platform (ContentVariant), and each
    variant becomes a Post on a connected account."""

    __tablename__ = "gtm_content_items"
    __table_args__ = (
        Index("ix_gtm_content_status", "business_id", "status", "planned_for"),
        Index("ix_gtm_content_campaign", "business_id", "campaign_id"),
    )

    campaign_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("gtm_campaigns.id", ondelete="SET NULL"))
    theme_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("gtm_content_themes.id", ondelete="SET NULL"))
    pillar: Mapped[str] = mapped_column(String(30), default="educate")
    intent: Mapped[str] = mapped_column(String(10), default="value")  # value | ask
    hook_type: Mapped[str | None] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(300))
    core_idea: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    cta: Mapped[str | None] = mapped_column(String(300))
    source_type: Mapped[str] = mapped_column(String(20), default="generated")  # generated | imported | manual
    source_ref: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    brand_book_version: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    checks: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    claims: Mapped[list[Any]] = mapped_column(default=list, server_default="[]")
    ai_generated: Mapped[bool] = mapped_column(default=True)
    edited_by_human: Mapped[bool] = mapped_column(default=False)
    simhash: Mapped[int | None] = mapped_column(BigInteger)
    planned_for: Mapped[datetime | None]
    created_by: Mapped[str] = mapped_column(String(100), default="agent:gtm.content")
    approved_by: Mapped[uuid.UUID | None]
    approved_at: Mapped[datetime | None]
    decision_id: Mapped[uuid.UUID | None]


class ContentVariant(IdMixin, TimestampMixin, TenantMixin, Base):
    """The native version of a content item for one platform and format."""

    __tablename__ = "gtm_content_variants"
    __table_args__ = (
        Index("uq_gtm_variant_platform", "content_id", "platform", "format", unique=True),
        Index("ix_gtm_variant_schedule", "business_id", "status", "scheduled_at"),
    )

    content_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("gtm_content_items.id", ondelete="CASCADE"))
    platform: Mapped[str] = mapped_column(String(30))
    format: Mapped[str] = mapped_column(String(40))
    title: Mapped[str | None] = mapped_column(String(300))
    text: Mapped[str] = mapped_column(Text, default="")
    thread: Mapped[list[Any]] = mapped_column(default=list, server_default="[]")  # extra parts (threads, carousels)
    media: Mapped[list[Any]] = mapped_column(default=list, server_default="[]")
    hashtags: Mapped[list[str]] = mapped_column(ARRAY(String(80)), default=list, server_default="{}")
    link_id: Mapped[uuid.UUID | None]
    spec: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    status: Mapped[str] = mapped_column(String(20), default="draft")
    scheduled_at: Mapped[datetime | None]
    account_id: Mapped[uuid.UUID | None]


class Asset(IdMixin, TenantMixin, Base):
    """The brand asset library: logos, templates and approved images and video."""

    __tablename__ = "gtm_assets"

    kind: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(200))
    storage_key: Mapped[str] = mapped_column(String(300))
    mime: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(default=0)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration_s: Mapped[float | None] = mapped_column(Float)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(50)), default=list, server_default="{}")
    alt_text: Mapped[str | None] = mapped_column(String(500))
    approved: Mapped[bool] = mapped_column(default=False)
    created_by: Mapped[str] = mapped_column(String(100), default="user")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ContentFingerprint(Base):
    """SimHash of everything published, across all businesses, so one customer can't get the
    platform's apps banned with duplicated spam. Holds hashes only: exempt from RLS."""

    __tablename__ = "content_fingerprints"
    __table_args__ = (Index("ix_content_fingerprints_time", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    simhash: Mapped[int] = mapped_column(BigInteger, index=True)
    band: Mapped[int] = mapped_column(Integer, index=True)  # top 16 bits, for candidate lookup
    platform: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
