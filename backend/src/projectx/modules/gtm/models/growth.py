from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Float, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class TrackedLink(IdMixin, TenantMixin, Base):
    """Every link the system posts is tracked, so analytics knows which post brought whom."""

    __tablename__ = "gtm_links"
    __table_args__ = (Index("ix_gtm_links_campaign", "business_id", "campaign_id"),)

    slug: Mapped[str] = mapped_column(String(16), unique=True)
    destination: Mapped[str] = mapped_column(String(2000))
    utm: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    label: Mapped[str | None] = mapped_column(String(200))
    campaign_id: Mapped[uuid.UUID | None]
    content_id: Mapped[uuid.UUID | None]
    variant_id: Mapped[uuid.UUID | None]
    post_id: Mapped[uuid.UUID | None]
    platform: Mapped[str | None] = mapped_column(String(30))
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LinkClick(IdMixin, TenantMixin, Base):
    """Raw click telemetry. No IP addresses are stored, only a salted daily visitor hash."""

    __tablename__ = "gtm_link_clicks"
    __table_args__ = (Index("ix_gtm_link_clicks_link", "business_id", "link_id", "clicked_at"),)

    link_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("gtm_links.id", ondelete="CASCADE"))
    click_id: Mapped[str] = mapped_column(String(32), unique=True)
    clicked_at: Mapped[datetime] = mapped_column(server_default=func.now())
    referrer_host: Mapped[str | None] = mapped_column(String(200))
    device: Mapped[str | None] = mapped_column(String(20))
    country: Mapped[str | None] = mapped_column(String(2))
    is_bot: Mapped[bool] = mapped_column(default=False)
    visitor_hash: Mapped[str | None] = mapped_column(String(32))
    contact_id: Mapped[uuid.UUID | None]


class LandingPage(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "gtm_landing_pages"
    __table_args__ = (Index("uq_gtm_landing_slug", "business_id", "slug", unique=True),)

    campaign_id: Mapped[uuid.UUID | None]
    slug: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20), default="lead_magnet")
    status: Mapped[str] = mapped_column(String(20), default="draft")
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    published_at: Mapped[datetime | None]
    views: Mapped[int] = mapped_column(Integer, default=0)
    submissions: Mapped[int] = mapped_column(Integer, default=0)
    experiment_id: Mapped[uuid.UUID | None]


class LandingVariant(IdMixin, TenantMixin, Base):
    __tablename__ = "gtm_landing_variants"
    __table_args__ = (Index("uq_gtm_landing_variant", "page_id", "key", unique=True),)

    page_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("gtm_landing_pages.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(8))
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    content: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    views: Mapped[int] = mapped_column(Integer, default=0)
    conversions: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PageEvent(IdMixin, TenantMixin, Base):
    __tablename__ = "gtm_page_events"
    __table_args__ = (Index("ix_gtm_page_events", "business_id", "page_id", "occurred_at"),)

    page_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("gtm_landing_pages.id", ondelete="CASCADE"))
    variant_key: Mapped[str | None] = mapped_column(String(8))
    kind: Mapped[str] = mapped_column(String(10))  # view | submit
    click_id: Mapped[str | None] = mapped_column(String(32))
    visitor_hash: Mapped[str | None] = mapped_column(String(32))
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PromoCode(IdMixin, TenantMixin, Base):
    __tablename__ = "gtm_promo_codes"
    __table_args__ = (Index("uq_gtm_promo_code", "business_id", "code", unique=True),)

    code: Mapped[str] = mapped_column(String(40))
    campaign_id: Mapped[uuid.UUID | None]
    description: Mapped[str | None] = mapped_column(String(200))
    uses: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
