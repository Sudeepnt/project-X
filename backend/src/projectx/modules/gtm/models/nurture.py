from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class Sequence(IdMixin, TimestampMixin, TenantMixin, Base):
    """A nurture sequence (email or WhatsApp), started by a funnel trigger."""

    __tablename__ = "gtm_sequences"
    __table_args__ = (Index("uq_gtm_sequence_key", "business_id", "key", unique=True),)

    key: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(200))
    channel: Mapped[str] = mapped_column(String(20), default="email")
    trigger: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    steps: Mapped[list[Any]] = mapped_column(default=list, server_default="[]")
    stop_on: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    active: Mapped[bool] = mapped_column(default=True)


class SequenceEnrollment(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "gtm_sequence_enrollments"
    __table_args__ = (
        Index(
            "uq_gtm_enrollment_active",
            "sequence_id",
            "contact_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_gtm_enrollment_due", "business_id", "status", "next_run_at"),
    )

    sequence_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("gtm_sequences.id", ondelete="CASCADE"))
    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(20), default="active")
    step_index: Mapped[int] = mapped_column(Integer, default=0)
    next_run_at: Mapped[datetime | None]
    stopped_reason: Mapped[str | None] = mapped_column(String(200))
    cause: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")


class Message(IdMixin, TenantMixin, Base):
    """Every outgoing message to a contact (sequence step, newsletter, reply, outreach)."""

    __tablename__ = "gtm_messages"
    __table_args__ = (
        Index("ix_gtm_messages_contact", "business_id", "contact_id", "created_at"),
        Index("ix_gtm_messages_status", "business_id", "status", "created_at"),
    )

    contact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    channel: Mapped[str] = mapped_column(String(20))
    kind: Mapped[str] = mapped_column(String(20))
    sequence_id: Mapped[uuid.UUID | None]
    enrollment_id: Mapped[uuid.UUID | None]
    issue_id: Mapped[uuid.UUID | None]
    subject: Mapped[str | None] = mapped_column(String(300))
    body_text: Mapped[str] = mapped_column(Text, default="")
    body_html: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    provider_id: Mapped[str | None] = mapped_column(String(300))
    error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None]
    opened_at: Mapped[datetime | None]
    clicked_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class NewsletterIssue(IdMixin, TimestampMixin, TenantMixin, Base):
    """A newsletter issue (for Bought: the daily digest)."""

    __tablename__ = "gtm_newsletter_issues"
    __table_args__ = (Index("ix_gtm_issues_status", "business_id", "status", "scheduled_at"),)

    title: Mapped[str] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(String(300))
    preheader: Mapped[str] = mapped_column(String(300), default="")
    sections: Mapped[list[Any]] = mapped_column(default=list, server_default="[]")
    body_html: Mapped[str] = mapped_column(Text, default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="draft")
    scheduled_at: Mapped[datetime | None]
    sent_at: Mapped[datetime | None]
    audience: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    stats: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    content_ids: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list, server_default="{}")
    decision_id: Mapped[uuid.UUID | None]
