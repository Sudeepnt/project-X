from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Index, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TenantMixin, TimestampMixin

# kind:
#   approval     the agent prepared an action; it runs after the founder says yes
#   manual_task  draft only: a person performs the action outside the platform, then marks it done
#   auto         the action ran on its own (Run / Run and tell); kept for the daily log and undo
#   promotion    an action type earned a clean streak; the founder decides whether it moves up
#   choice       the founder picks one of several options (e.g. a goal is behind pace)
DECISION_KINDS = ("approval", "manual_task", "auto", "promotion", "choice")
DECISION_STATUSES = (
    "pending",
    "approved",
    "rejected",
    "expired",
    "cancelled",
    "executing",
    "executed",
    "failed",
    "done",
    "undone",
)
OPEN_STATUSES = ("pending",)


class Decision(IdMixin, TimestampMixin, TenantMixin, Base):
    """A decision card in the founder's queue, and the audit record of what happened after."""

    __tablename__ = "decisions"
    __table_args__ = (
        Index("ix_decisions_queue", "business_id", "status", "priority", "created_at"),
        Index("ix_decisions_subject", "business_id", "subject_type", "subject_id"),
    )

    action_type: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    level: Mapped[int] = mapped_column(SmallInteger)
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text, default="")
    why_now: Mapped[str | None] = mapped_column(Text)
    forecast: Mapped[dict[str, Any] | None]
    cost_minor: Mapped[int | None]
    currency: Mapped[str | None] = mapped_column(String(3))
    if_no: Mapped[str | None] = mapped_column(Text)
    undo: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    preview: Mapped[dict[str, Any] | None]
    options: Mapped[list[Any] | None]
    reasons: Mapped[list[Any]] = mapped_column(default=list, server_default="[]")
    subject_type: Mapped[str | None] = mapped_column(String(50))
    subject_id: Mapped[uuid.UUID | None]
    priority: Mapped[int] = mapped_column(SmallInteger, default=50)
    expires_at: Mapped[datetime | None]
    requested_by: Mapped[str] = mapped_column(String(100))
    decided_by: Mapped[uuid.UUID | None]
    decided_at: Mapped[datetime | None]
    reason_code: Mapped[str | None] = mapped_column(String(40))
    note: Mapped[str | None] = mapped_column(Text)
    edited: Mapped[bool] = mapped_column(default=False)
    chosen_option: Mapped[str | None] = mapped_column(String(60))
    executed_at: Mapped[datetime | None]
    result: Mapped[dict[str, Any] | None]
    undo_payload: Mapped[dict[str, Any] | None]
    correlation_id: Mapped[uuid.UUID | None]
