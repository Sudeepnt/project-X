"""The business twin, version zero: the shared map of a business's people, deals and money.

Every module reads and writes these same entities. GTM fills the funnel; later modules
(support, billing, finance) work on the same contacts, deals and payments.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TenantMixin, TimestampMixin

# The funnel is a bowtie: it keeps going after the sale (Winning by Design).
STAGES = (
    "audience",
    "subscriber",
    "lead",
    "mql",
    "sql",
    "opportunity",
    "customer",
    "active",
    "expansion",
    "advocate",
)
SIDE_STATES = ("disqualified", "recycled", "at_risk", "churned", "do_not_contact")
STATUSES = (
    "new",
    "contacted",
    "engaged",
    "no_response",
    "meeting_scheduled",
    "proposal_sent",
    "won",
    "lost",
)
CUSTOMER_TYPES = ("one_time", "retainer", "subscription")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Contact(IdMixin, TimestampMixin, TenantMixin, Base):
    """A person the business knows. Every contact always has a stage and a status."""

    __tablename__ = "contacts"
    __table_args__ = (
        CheckConstraint(_in("stage", STAGES), name="stage_valid"),
        CheckConstraint(_in("status", STATUSES), name="status_valid"),
        CheckConstraint(f"side_state IS NULL OR {_in('side_state', SIDE_STATES)}", name="side_state_valid"),
        Index("uq_contacts_email", "business_id", "email", unique=True, postgresql_where=text("email IS NOT NULL")),
        Index("ix_contacts_funnel", "business_id", "stage", "status"),
        Index("ix_contacts_activity", "business_id", "last_activity_at"),
        Index("ix_contacts_handles", "handles", postgresql_using="gin"),
    )

    email: Mapped[str | None] = mapped_column(CITEXT)
    phone: Mapped[str | None] = mapped_column(String(32))
    name: Mapped[str | None] = mapped_column(String(200))
    company: Mapped[str | None] = mapped_column(String(200))
    job_title: Mapped[str | None] = mapped_column(String(200))
    handles: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    stage: Mapped[str] = mapped_column(String(20))
    side_state: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30), default="new")
    status_reason: Mapped[str | None] = mapped_column(String(300))
    customer_type: Mapped[str | None] = mapped_column(String(20))
    source: Mapped[str | None] = mapped_column(String(50))
    first_touch: Mapped[dict[str, Any] | None]
    last_touch: Mapped[dict[str, Any] | None]
    first_campaign_id: Mapped[uuid.UUID | None]
    last_campaign_id: Mapped[uuid.UUID | None]
    lead_score: Mapped[int] = mapped_column(Integer, default=0)
    owner_type: Mapped[str] = mapped_column(String(10), default="ai")
    owner_user_id: Mapped[uuid.UUID | None]
    next_action: Mapped[str | None] = mapped_column(String(300))
    next_action_at: Mapped[datetime | None]
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(50)), default=list, server_default="{}")
    attributes: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    email_status: Mapped[str] = mapped_column(String(20), default="unknown")
    unsubscribed_at: Mapped[datetime | None]
    last_activity_at: Mapped[datetime | None]
    stage_entered_at: Mapped[datetime] = mapped_column(server_default=func.now())
    lifetime_value_minor: Mapped[int] = mapped_column(default=0)
    referral_code: Mapped[str | None] = mapped_column(String(16), unique=True)
    referred_by_id: Mapped[uuid.UUID | None]
    erased_at: Mapped[datetime | None]


class ContactStageChange(IdMixin, TenantMixin, Base):
    """Every stage or status change, with what caused it. The learning loop learns from these."""

    __tablename__ = "contact_stage_changes"
    __table_args__ = (
        Index("ix_stage_changes_contact", "business_id", "contact_id", "changed_at"),
        Index("ix_stage_changes_time", "business_id", "to_stage", "changed_at"),
    )

    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"))
    from_stage: Mapped[str | None] = mapped_column(String(20))
    to_stage: Mapped[str] = mapped_column(String(20))
    from_status: Mapped[str | None] = mapped_column(String(30))
    to_status: Mapped[str] = mapped_column(String(30))
    from_side_state: Mapped[str | None] = mapped_column(String(20))
    to_side_state: Mapped[str | None] = mapped_column(String(20))
    cause_type: Mapped[str] = mapped_column(String(30))
    cause_id: Mapped[uuid.UUID | None]
    actor: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str | None] = mapped_column(String(300))
    changed_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Touch(IdMixin, TenantMixin, Base):
    """One interaction between a contact and the business (a click, a reply, a payment…)."""

    __tablename__ = "touches"
    __table_args__ = (
        Index("ix_touches_contact", "business_id", "contact_id", "occurred_at"),
        Index("ix_touches_campaign", "business_id", "campaign_id", "occurred_at"),
    )

    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(30))
    channel: Mapped[str | None] = mapped_column(String(30))
    campaign_id: Mapped[uuid.UUID | None]
    content_id: Mapped[uuid.UUID | None]
    post_id: Mapped[uuid.UUID | None]
    link_id: Mapped[uuid.UUID | None]
    points: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Consent(IdMixin, TenantMixin, Base):
    """Proof of permission to contact someone, per channel and purpose. Append-style history."""

    __tablename__ = "consents"
    __table_args__ = (Index("ix_consents_contact", "business_id", "contact_id", "channel", "recorded_at"),)

    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(String(20))
    purpose: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(10))
    source: Mapped[str] = mapped_column(String(50))
    evidence: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    recorded_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Pipeline(IdMixin, TimestampMixin, TenantMixin, Base):
    """A deal pipeline, e.g. "Sales" or Bought's "Advertisers"."""

    __tablename__ = "pipelines"
    __table_args__ = (Index("uq_pipelines_key", "business_id", "key", unique=True),)

    key: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(100))
    stages: Mapped[list[Any]] = mapped_column(default=list)  # [{key,label,probability,kind}]
    is_default: Mapped[bool] = mapped_column(default=False)


class Deal(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "deals"
    __table_args__ = (Index("ix_deals_board", "business_id", "pipeline_id", "status", "stage_key"),)

    pipeline_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pipelines.id", ondelete="CASCADE"))
    contact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(200))
    stage_key: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(10), default="open")
    value_minor: Mapped[int] = mapped_column(default=0)
    currency: Mapped[str] = mapped_column(String(3))
    deal_type: Mapped[str] = mapped_column(String(20), default="one_time")
    expected_close: Mapped[date | None]
    won_at: Mapped[datetime | None]
    lost_at: Mapped[datetime | None]
    lost_reason: Mapped[str | None] = mapped_column(String(300))
    campaign_id: Mapped[uuid.UUID | None]
    owner_type: Mapped[str] = mapped_column(String(10), default="human")
    owner_user_id: Mapped[uuid.UUID | None]
    next_action: Mapped[str | None] = mapped_column(String(300))
    next_action_at: Mapped[datetime | None]
    notes: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    stage_entered_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Payment(IdMixin, TimestampMixin, TenantMixin, Base):
    """Money the business received (from its own payment provider), matched to contact and campaign."""

    __tablename__ = "payments"
    __table_args__ = (
        Index("uq_payments_external", "business_id", "provider", "external_id", unique=True),
        Index("ix_payments_time", "business_id", "paid_at"),
    )

    contact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    deal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("deals.id", ondelete="SET NULL"))
    provider: Mapped[str] = mapped_column(String(30))
    external_id: Mapped[str] = mapped_column(String(200))
    amount_minor: Mapped[int]
    currency: Mapped[str] = mapped_column(String(3))
    kind: Mapped[str] = mapped_column(String(20), default="one_time")
    status: Mapped[str] = mapped_column(String(20), default="succeeded")
    paid_at: Mapped[datetime]
    promo_code: Mapped[str | None] = mapped_column(String(60))
    campaign_id: Mapped[uuid.UUID | None]
    description: Mapped[str | None] = mapped_column(String(300))
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
