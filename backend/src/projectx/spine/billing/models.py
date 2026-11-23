from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, TimestampMixin


class Subscription(TimestampMixin, Base):
    """What a business pays Project X. One bill per business, whatever modules it uses."""

    __tablename__ = "subscriptions"

    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True)
    plan: Mapped[str] = mapped_column(String(30), default="free")
    status: Mapped[str] = mapped_column(String(20), default="active")
    provider: Mapped[str | None] = mapped_column(String(20))
    provider_customer_id: Mapped[str | None] = mapped_column(String(200), index=True)
    provider_subscription_id: Mapped[str | None] = mapped_column(String(200), index=True)
    current_period_start: Mapped[datetime | None]
    current_period_end: Mapped[datetime | None]
    cancel_at_period_end: Mapped[bool] = mapped_column(default=False)
    ad_spend_share_bps: Mapped[int] = mapped_column(Integer, default=0)


class UsageCounter(Base):
    """Metered usage per month (posts, AI credits, contacts…), checked against plan limits."""

    __tablename__ = "usage_counters"

    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True)
    metric: Mapped[str] = mapped_column(String(50), primary_key=True)
    period: Mapped[str] = mapped_column(String(7), primary_key=True)  # YYYY-MM
    value: Mapped[int] = mapped_column(default=0)


class BillingEvent(Base):
    """Every provider webhook we accepted, once. Replays and retries are recognised by their id."""

    __tablename__ = "billing_events"

    event_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    provider: Mapped[str] = mapped_column(String(20))
    type: Mapped[str] = mapped_column(String(80))
    business_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("businesses.id", ondelete="SET NULL"), index=True)
    outcome: Mapped[str] = mapped_column(String(200), default="")
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())
