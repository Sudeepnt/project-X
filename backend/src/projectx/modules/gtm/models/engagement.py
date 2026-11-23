from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import Float, Index, Integer, SmallInteger, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TenantMixin, TimestampMixin

INBOX_CATEGORIES = ("common_question", "sales_intent", "support", "praise", "negative", "sensitive", "spam", "other")


class InboxItem(IdMixin, TimestampMixin, TenantMixin, Base):
    """One shared inbox for comments, DMs, mentions and email replies across every channel."""

    __tablename__ = "gtm_inbox_items"
    __table_args__ = (
        Index("uq_gtm_inbox_external", "business_id", "platform", "external_id", unique=True),
        Index("ix_gtm_inbox_queue", "business_id", "status", "priority", "received_at"),
    )

    platform: Mapped[str] = mapped_column(String(30))
    kind: Mapped[str] = mapped_column(String(20))
    external_id: Mapped[str] = mapped_column(String(200))
    thread_id: Mapped[str | None] = mapped_column(String(200))
    post_id: Mapped[uuid.UUID | None]
    account_id: Mapped[uuid.UUID | None]
    author_handle: Mapped[str | None] = mapped_column(String(200))
    author_name: Mapped[str | None] = mapped_column(String(200))
    contact_id: Mapped[uuid.UUID | None]
    text: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())
    sentiment: Mapped[float | None] = mapped_column(Float)
    category: Mapped[str | None] = mapped_column(String(30))
    priority: Mapped[int] = mapped_column(SmallInteger, default=50)
    status: Mapped[str] = mapped_column(String(20), default="open")
    ai_draft: Mapped[str | None] = mapped_column(Text)
    reply_text: Mapped[str | None] = mapped_column(Text)
    replied_at: Mapped[datetime | None]
    assigned_user_id: Mapped[uuid.UUID | None]
    auto_replied: Mapped[bool] = mapped_column(default=False)
    decision_id: Mapped[uuid.UUID | None]
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")


class CommunityMember(IdMixin, TimestampMixin, TenantMixin, Base):
    """A member of the business's community (Discord first), linked to the funnel."""

    __tablename__ = "gtm_community_members"
    __table_args__ = (Index("uq_gtm_community_member", "business_id", "platform", "external_user_id", unique=True),)

    platform: Mapped[str] = mapped_column(String(20), default="discord")
    external_user_id: Mapped[str] = mapped_column(String(100))
    handle: Mapped[str | None] = mapped_column(String(200))
    contact_id: Mapped[uuid.UUID | None]
    joined_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_active_at: Mapped[datetime | None]
    messages_count: Mapped[int] = mapped_column(Integer, default=0)
    roles: Mapped[list[str]] = mapped_column(ARRAY(String(60)), default=list, server_default="{}")
    onboarding_answers: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    is_ambassador: Mapped[bool] = mapped_column(default=False)
    left_at: Mapped[datetime | None]


class CommunityDaily(IdMixin, TenantMixin, Base):
    __tablename__ = "gtm_community_daily"
    __table_args__ = (Index("uq_gtm_community_day", "business_id", "platform", "day", unique=True),)

    platform: Mapped[str] = mapped_column(String(20), default="discord")
    day: Mapped[date]
    active_members: Mapped[int] = mapped_column(Integer, default=0)
    messages: Mapped[int] = mapped_column(Integer, default=0)
    new_members: Mapped[int] = mapped_column(Integer, default=0)
    new_members_posted: Mapped[int] = mapped_column(Integer, default=0)
    total_members: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(Text)
