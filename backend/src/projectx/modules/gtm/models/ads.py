from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class AdAccount(IdMixin, TimestampMixin, TenantMixin, Base):
    """The customer's own ad account. Meta and Google bill the customer directly; ad spend is
    never inside our price. Our cap is mirrored to the account's own spending limit."""

    __tablename__ = "gtm_ad_accounts"
    __table_args__ = (Index("uq_gtm_ad_account", "business_id", "platform", "external_id", unique=True),)

    platform: Mapped[str] = mapped_column(String(20))  # meta | google
    external_id: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(20), default="connected")
    secret_id: Mapped[uuid.UUID | None]
    monthly_cap_minor: Mapped[int] = mapped_column(default=0)
    platform_spend_limit_minor: Mapped[int | None]
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")


class AdCampaign(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "gtm_ad_campaigns"
    __table_args__ = (Index("ix_gtm_ad_campaigns", "business_id", "status"),)

    ad_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("gtm_ad_accounts.id", ondelete="CASCADE"))
    campaign_id: Mapped[uuid.UUID | None]
    platform: Mapped[str] = mapped_column(String(20))
    external_id: Mapped[str | None] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    objective: Mapped[str] = mapped_column(String(30), default="leads")
    audience_kind: Mapped[str] = mapped_column(String(20), default="retargeting")  # retargeting|lookalike|cold
    daily_budget_minor: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    special_category: Mapped[str | None] = mapped_column(String(30))  # housing|employment|credit|politics
    creative_content_ids: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list, server_default="{}")
    rules: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    target_cpl_minor: Mapped[int | None]
    paused_reason: Mapped[str | None] = mapped_column(Text)
    starts_on: Mapped[date | None]
    ends_on: Mapped[date | None]
    last_synced_at: Mapped[datetime | None]


class AdMetricDaily(IdMixin, TenantMixin, Base):
    __tablename__ = "gtm_ad_metrics_daily"
    __table_args__ = (Index("uq_gtm_ad_metric_day", "ad_campaign_id", "day", unique=True),)

    ad_campaign_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("gtm_ad_campaigns.id", ondelete="CASCADE"))
    day: Mapped[date]
    spend_minor: Mapped[int] = mapped_column(default=0)
    impressions: Mapped[int] = mapped_column(default=0)
    clicks: Mapped[int] = mapped_column(default=0)
    leads: Mapped[int] = mapped_column(Integer, default=0)
    purchases: Mapped[int] = mapped_column(Integer, default=0)
    revenue_minor: Mapped[int] = mapped_column(default=0)
    notes: Mapped[str | None] = mapped_column(Text)
