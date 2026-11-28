from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TenantMixin, TimestampMixin

DEFAULT_WORKING_HOURS: dict[str, Any] = {day: [["09:00", "17:00"]] for day in ("mon", "tue", "wed", "thu", "fri")} | {
    "sat": [],
    "sun": [],
}


class CalendarSettings(TimestampMixin, Base):
    """When the business can meet. Times are local to the business time zone."""

    __tablename__ = "calendar_settings"

    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True)
    # {"mon": [["09:00", "12:30"], ["13:30", "17:00"]], ..., "sun": []}
    working_hours: Mapped[dict[str, Any]] = mapped_column(default=lambda: dict(DEFAULT_WORKING_HOURS))
    buffer_minutes: Mapped[int] = mapped_column(Integer, default=10)
    min_notice_minutes: Mapped[int] = mapped_column(Integer, default=120)
    slot_step_minutes: Mapped[int] = mapped_column(Integer, default=30)
    default_duration_minutes: Mapped[int] = mapped_column(Integer, default=30)


class CalendarEvent(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "calendar_events"
    __table_args__ = (Index("ix_calendar_events_range", "business_id", "starts_at", "ends_at"),)

    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str | None] = mapped_column(String(300))
    kind: Mapped[str] = mapped_column(String(20), default="meeting")  # meeting | focus | other
    starts_at: Mapped[datetime]
    ends_at: Mapped[datetime]
    status: Mapped[str] = mapped_column(String(20), default="scheduled")  # scheduled | cancelled
    # [{"email": "...", "name": "..."}]. Personal data: erased with the contact.
    attendees: Mapped[list[Any]] = mapped_column(default=list)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    # RFC 5545 SEQUENCE: goes up with every change so calendar apps replace the old copy.
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    invite_sequence_sent: Mapped[int | None] = mapped_column(Integer)
    # Copies on connected calendars: {"<connection id>": "<provider event id>"}
    external_refs: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    source: Mapped[str] = mapped_column(String(20), default="person")  # person | agent
    created_by: Mapped[uuid.UUID | None]
    cancelled_at: Mapped[datetime | None]


class Reminder(IdMixin, TenantMixin, TimestampMixin, Base):
    """Anything worth remembering: a meeting, a renewal, a deadline, a payment, a follow-up."""

    __tablename__ = "reminders"
    __table_args__ = (
        Index("ix_reminders_due", "business_id", "status", "next_notify_at"),
        Index("uq_reminders_dedupe", "business_id", "dedupe_key", unique=True),
    )

    title: Mapped[str] = mapped_column(String(200))
    notes: Mapped[str] = mapped_column(Text, default="")
    # meeting | renewal | deadline | payment | follow_up | task | other
    kind: Mapped[str] = mapped_column(String(20), default="other")
    importance: Mapped[str] = mapped_column(String(10), default="normal")  # low | normal | high
    due_at: Mapped[datetime]
    all_day: Mapped[bool] = mapped_column(default=False)
    # Minutes before due_at to remind, e.g. [43200, 10080, 1440] for 30, 7 and 1 day before.
    notify_before_minutes: Mapped[list[Any]] = mapped_column(default=list)
    repeat: Mapped[str] = mapped_column(String(10), default="none")  # none | daily | weekly | monthly | yearly
    next_notify_at: Mapped[datetime | None]
    last_notified_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(String(10), default="active")  # active | done | dismissed
    source: Mapped[str] = mapped_column(String(20), default="person")  # person | agent | calendar
    event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("calendar_events.id", ondelete="CASCADE"))
    dedupe_key: Mapped[str | None] = mapped_column(String(120))
    created_by: Mapped[uuid.UUID | None]
    completed_at: Mapped[datetime | None]


class CalendarConnection(IdMixin, TenantMixin, TimestampMixin, Base):
    """A Google or Microsoft calendar the business connected. Tokens live in the vault, never here."""

    __tablename__ = "calendar_connections"

    provider: Mapped[str] = mapped_column(String(20))  # google | microsoft
    label: Mapped[str] = mapped_column(String(200), default="")  # the account address, shown to the owner
    secret_id: Mapped[uuid.UUID]
    status: Mapped[str] = mapped_column(String(20), default="connected")  # connected | error
    push_events: Mapped[bool] = mapped_column(default=True)  # copy our events onto this calendar
    last_synced_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    connected_by: Mapped[uuid.UUID | None]


class CalendarBusy(IdMixin, TenantMixin, Base):
    """Busy time read from a connected calendar. Only start and end: no titles, no attendees."""

    __tablename__ = "calendar_busy"
    __table_args__ = (Index("ix_calendar_busy_range", "business_id", "starts_at", "ends_at"),)

    connection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calendar_connections.id", ondelete="CASCADE"))
    starts_at: Mapped[datetime]
    ends_at: Mapped[datetime]
