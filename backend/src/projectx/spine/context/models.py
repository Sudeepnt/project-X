from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Computed, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TenantMixin, TimestampMixin

NO_USER = "00000000-0000-0000-0000-000000000000"


class ContextEntry(IdMixin, TenantMixin, Base):
    """One thing the assistant knows, kept in our database and nowhere else.

    Entries are never edited in place: a change writes a new row and marks the old one superseded,
    so the full history of what it believed, and when, is kept (Git-like, but queryable). A person
    can see every entry, correct it, pin it, or make it forget.
    """

    __tablename__ = "context_entries"
    __table_args__ = (
        Index(
            "uq_context_active_key",
            "business_id",
            text(f"coalesce(user_id, '{NO_USER}'::uuid)"),
            "key",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_context_search", "search", postgresql_using="gin"),
        Index("ix_context_scope", "business_id", "user_id", "status", "kind"),
    )

    user_id: Mapped[uuid.UUID | None]  # None: true for the whole business
    # preference | person | fact | routine | commitment
    kind: Mapped[str] = mapped_column(String(20))
    key: Mapped[str] = mapped_column(String(120))  # e.g. "meetings.morning_start", "person.priya"
    value: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20), default="assistant")  # assistant | person | capture | system
    evidence: Mapped[str] = mapped_column(Text, default="")  # the words it was learned from
    confidence: Mapped[float] = mapped_column(default=0.8)
    pinned: Mapped[bool] = mapped_column(default=False)  # always in the prompt
    status: Mapped[str] = mapped_column(String(12), default="active")  # active | superseded | forgotten
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("context_entries.id", ondelete="SET NULL"))
    data: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")  # e.g. {"reminder_id": ...}
    confirmed_at: Mapped[datetime] = mapped_column(server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    ended_at: Mapped[datetime | None]
    search: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', coalesce(key, '') || ' ' || coalesce(value, ''))", persisted=True),
    )


class AssistantSettings(TimestampMixin, Base):
    """How and when the assistant may reach one person on its own."""

    __tablename__ = "assistant_settings"

    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    brief_time: Mapped[str] = mapped_column(String(5), default="07:30")  # local time of the morning brief
    brief_days: Mapped[list[Any]] = mapped_column(default=lambda: ["mon", "tue", "wed", "thu", "fri"])
    quiet_start: Mapped[str] = mapped_column(String(5), default="21:00")
    quiet_end: Mapped[str] = mapped_column(String(5), default="07:00")
    channels: Mapped[list[Any]] = mapped_column(default=lambda: ["app", "email"])
    max_nudges_per_day: Mapped[int] = mapped_column(default=6)
    # What it already told the person, so it never repeats itself: {"signal key": "ISO time told"}
    told: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")


class AgentWakeup(IdMixin, TenantMixin, TimestampMixin, Base):
    """A moment the assistant decided to wake up and look at someone's day."""

    __tablename__ = "agent_wakeups"
    __table_args__ = (
        Index("ix_agent_wakeups_due", "business_id", "status", "wake_at"),
        Index("uq_agent_wakeups_key", "business_id", "dedupe_key", unique=True),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    wake_at: Mapped[datetime]
    kind: Mapped[str] = mapped_column(String(20))  # brief | check
    reason: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending | done | skipped
    dedupe_key: Mapped[str] = mapped_column(String(160))
    result: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    done_at: Mapped[datetime | None]
