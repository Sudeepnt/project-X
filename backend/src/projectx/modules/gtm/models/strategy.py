from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TenantMixin, TimestampMixin

GOAL_TYPES = ("awareness", "demand", "sales", "partnerships", "retention", "reputation")
GOAL_STATUSES = ("draft", "active", "paused", "achieved", "missed", "stopped", "maintenance")
GROWTH_APPROACHES = ("content_community", "sales_referrals", "partner_led", "product_led")


class Goal(IdMixin, TimestampMixin, TenantMixin, Base):
    """One measurable outcome with a deadline and a budget. Only one main goal runs at a time.
    The goal is measured inside the funnel: "50 paying customers" means 50 contacts at the
    Customer stage."""

    __tablename__ = "gtm_goals"
    __table_args__ = (
        Index("uq_gtm_goals_one_active", "business_id", unique=True, postgresql_where=text("status = 'active'")),
    )

    type: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(200))
    metric: Mapped[str] = mapped_column(String(50))
    unit: Mapped[str] = mapped_column(String(20), default="count")  # count | currency_minor | percent | rating
    stage: Mapped[str | None] = mapped_column(String(20))  # funnel stage where it's measured
    baseline_value: Mapped[float] = mapped_column(Numeric(18, 4, asdecimal=False), default=0)
    target_value: Mapped[float] = mapped_column(Numeric(18, 4, asdecimal=False))
    current_value: Mapped[float] = mapped_column(Numeric(18, 4, asdecimal=False), default=0)
    start_date: Mapped[date]
    deadline: Mapped[date]
    budget_minor: Mapped[int] = mapped_column(default=0)
    currency: Mapped[str] = mapped_column(String(3))
    ai_credit_budget_cents: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    growth_approach: Mapped[str] = mapped_column(String(30), default="content_community")
    approval_mode: Mapped[str] = mapped_column(String(20), default="approve_all")
    guardrail: Mapped[dict[str, Any] | None]
    stop_rules: Mapped[list[Any]] = mapped_column(default=list, server_default="[]")
    forecast: Mapped[dict[str, Any] | None]
    pace: Mapped[dict[str, Any] | None]
    diagnosis: Mapped[dict[str, Any] | None]
    spent_minor: Mapped[int] = mapped_column(default=0)
    ended_at: Mapped[datetime | None]
    end_reason: Mapped[str | None] = mapped_column(String(300))


class Restriction(IdMixin, TenantMixin, Base):
    """A limit the system must respect: time, money, volume, approval, brand or guardrail."""

    __tablename__ = "gtm_restrictions"

    goal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("gtm_goals.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(40))
    params: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    active: Mapped[bool] = mapped_column(default=True)
    note: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class Campaign(IdMixin, TimestampMixin, TenantMixin, Base):
    """A running instance of a playbook from the campaign library, serving one goal."""

    __tablename__ = "gtm_campaigns"
    __table_args__ = (Index("ix_gtm_campaigns_status", "business_id", "status"),)

    goal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("gtm_goals.id", ondelete="SET NULL"))
    playbook_key: Mapped[str] = mapped_column(String(60))
    role: Mapped[str] = mapped_column(String(12), default="main")  # main | supporting
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="draft")
    starts_on: Mapped[date | None]
    ends_on: Mapped[date | None]
    budget_minor: Mapped[int] = mapped_column(default=0)
    budget_split: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    channels: Mapped[list[str]] = mapped_column(ARRAY(String(30)), default=list, server_default="{}")
    content_mix: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    frequency: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    value_ratio: Mapped[float] = mapped_column(Numeric(6, 2, asdecimal=False), default=3)
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    metrics: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    brief: Mapped[str] = mapped_column(Text, default="")
    stop_reason: Mapped[str | None] = mapped_column(String(300))
