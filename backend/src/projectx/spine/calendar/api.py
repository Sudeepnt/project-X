"""Calendar and reminder endpoints, scoped to one business."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select

from projectx.api.deps import BusinessDep, EditorDep
from projectx.kernel.errors import NotFound, ValidationFailed
from projectx.kernel.time import utcnow
from projectx.spine.calendar import service as cal
from projectx.spine.calendar.models import CalendarEvent, Reminder
from projectx.spine.calendar.timing import REPEATS, next_notify_at, validate_working_hours
from projectx.spine.ledger.service import Actor

router = APIRouter(prefix="/api/v1/b/{business_id}", tags=["calendar"])

Kind = Literal["meeting", "renewal", "deadline", "payment", "follow_up", "task", "other"]
Repeat = Literal["none", "daily", "weekly", "monthly", "yearly"]
Importance = Literal["low", "normal", "high"]

# ---------------------------------------------------------------- settings


class SettingsIn(BaseModel):
    working_hours: dict[str, list[list[str]]] | None = None
    buffer_minutes: int | None = Field(default=None, ge=0, le=240)
    min_notice_minutes: int | None = Field(default=None, ge=0, le=14 * 1440)
    slot_step_minutes: int | None = Field(default=None, ge=5, le=240)
    default_duration_minutes: int | None = Field(default=None, ge=5, le=8 * 60)


@router.get("/calendar/settings")
async def get_settings(ctx: BusinessDep) -> dict[str, Any]:
    return cal.settings_view(await cal.get_settings(ctx.session, ctx.business_id))


@router.put("/calendar/settings")
async def put_settings(body: SettingsIn, ctx: EditorDep) -> dict[str, Any]:
    s = await cal.get_settings(ctx.session, ctx.business_id)
    if body.working_hours is not None:
        try:
            s.working_hours = validate_working_hours(body.working_hours)
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
    for field in ("buffer_minutes", "min_notice_minutes", "slot_step_minutes", "default_duration_minutes"):
        value = getattr(body, field)
        if value is not None:
            setattr(s, field, value)
    return cal.settings_view(s)


# ---------------------------------------------------------------- events


class AttendeeIn(BaseModel):
    email: EmailStr
    name: str | None = Field(default=None, max_length=120)


class EventIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    starts_at: datetime
    ends_at: datetime | None = None
    duration_minutes: int | None = Field(default=None, ge=5, le=14 * 1440)
    description: str = Field(default="", max_length=5000)
    location: str | None = Field(default=None, max_length=300)
    kind: Literal["meeting", "focus", "other"] = "meeting"
    attendees: list[AttendeeIn] = Field(default_factory=list, max_length=50)
    contact_id: uuid.UUID | None = None
    remind_before_minutes: list[int] | None = Field(default=None, max_length=5)
    allow_overlap: bool = False


class RescheduleIn(BaseModel):
    starts_at: datetime
    ends_at: datetime | None = None
    allow_overlap: bool = False


def _with_decision(e: CalendarEvent, decision_id: uuid.UUID | None) -> dict[str, Any]:
    return cal.event_view(e) | {"invite_decision_id": str(decision_id) if decision_id else None}


@router.get("/calendar/events")
async def list_events(
    ctx: BusinessDep, start: datetime | None = None, end: datetime | None = None, include_cancelled: bool = False
) -> dict[str, Any]:
    tz = ctx.business.timezone
    a = cal._aware(start, tz) if start else utcnow() - timedelta(days=1)
    b = cal._aware(end, tz) if end else a + timedelta(days=31)
    if b - a > timedelta(days=93):
        raise ValidationFailed("Ask for at most three months at a time")
    stmt = select(CalendarEvent).where(CalendarEvent.starts_at < b, CalendarEvent.ends_at > a)
    if not include_cancelled:
        stmt = stmt.where(CalendarEvent.status == "scheduled")
    rows = (await ctx.session.execute(stmt.order_by(CalendarEvent.starts_at).limit(500))).scalars()
    return {"items": [cal.event_view(e) for e in rows], "timezone": tz}


@router.post("/calendar/events", status_code=201)
async def create_event(body: EventIn, ctx: EditorDep) -> dict[str, Any]:
    event, decision_id = await cal.schedule_event(
        ctx.session,
        ctx.business,
        title=body.title,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
        duration_minutes=body.duration_minutes,
        description=body.description,
        location=body.location,
        kind=body.kind,
        attendees=[a.model_dump() for a in body.attendees],
        contact_id=body.contact_id,
        remind_before_minutes=body.remind_before_minutes,
        allow_overlap=body.allow_overlap,
        actor=Actor.user(ctx.user.id),
        created_by=ctx.user.id,
    )
    return _with_decision(event, decision_id)


@router.get("/calendar/events/{event_id}")
async def get_event(event_id: uuid.UUID, ctx: BusinessDep) -> dict[str, Any]:
    event = await ctx.session.get(CalendarEvent, event_id)
    if event is None:
        raise NotFound("Event not found")
    return cal.event_view(event)


@router.post("/calendar/events/{event_id}/reschedule")
async def reschedule(event_id: uuid.UUID, body: RescheduleIn, ctx: EditorDep) -> dict[str, Any]:
    event, decision_id = await cal.reschedule_event(
        ctx.session,
        ctx.business,
        event_id,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
        allow_overlap=body.allow_overlap,
        actor=Actor.user(ctx.user.id),
    )
    return _with_decision(event, decision_id)


@router.post("/calendar/events/{event_id}/cancel")
async def cancel(event_id: uuid.UUID, ctx: EditorDep) -> dict[str, Any]:
    event, decision_id = await cal.cancel_event(ctx.session, ctx.business, event_id, actor=Actor.user(ctx.user.id))
    return _with_decision(event, decision_id)


@router.get("/calendar/free")
async def free_time(
    ctx: BusinessDep,
    start: datetime | None = None,
    end: datetime | None = None,
    duration_minutes: int | None = None,
    limit: int = 20,
    per_day: int | None = None,
) -> dict[str, Any]:
    if duration_minutes is not None and not 5 <= duration_minutes <= 8 * 60:
        raise ValidationFailed("Duration is between 5 minutes and 8 hours")
    slots = await cal.find_free_time(
        ctx.session,
        ctx.business,
        start=start,
        end=end,
        duration_minutes=duration_minutes,
        limit=max(1, min(limit, 100)),
        per_day=per_day,
    )
    return {
        "timezone": ctx.business.timezone,
        "slots": [{"starts_at": s.start.isoformat(), "ends_at": s.end.isoformat()} for s in slots],
    }


# ---------------------------------------------------------------- reminders


class ReminderIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    due_at: datetime
    kind: Kind = "other"
    notes: str = Field(default="", max_length=4000)
    importance: Importance = "normal"
    all_day: bool = False
    notify_before_minutes: list[int] | None = Field(default=None, max_length=5)
    repeat: Repeat = "none"


class ReminderPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    due_at: datetime | None = None
    kind: Kind | None = None
    notes: str | None = Field(default=None, max_length=4000)
    importance: Importance | None = None
    all_day: bool | None = None
    notify_before_minutes: list[int] | None = Field(default=None, max_length=5)
    repeat: Repeat | None = None


class CaptureIn(BaseModel):
    text: str = Field(min_length=3, max_length=12_000)


class SnoozeIn(BaseModel):
    minutes: int = Field(ge=5, le=30 * 1440)


async def _reminder(ctx: BusinessDep, reminder_id: uuid.UUID) -> Reminder:
    r = await ctx.session.scalar(select(Reminder).where(Reminder.id == reminder_id).with_for_update())
    if r is None:
        raise NotFound("Reminder not found")
    return r


@router.get("/reminders")
async def list_reminders(
    ctx: BusinessDep, status: str = "active", kind: str | None = None, before: datetime | None = None
) -> dict[str, Any]:
    stmt = select(Reminder).where(Reminder.status.in_(status.split(",")))
    if kind:
        stmt = stmt.where(Reminder.kind.in_(kind.split(",")))
    if before:
        stmt = stmt.where(Reminder.due_at < cal._aware(before, ctx.business.timezone))
    rows = (await ctx.session.execute(stmt.order_by(Reminder.due_at).limit(500))).scalars()
    return {"items": [cal.reminder_view(r) for r in rows]}


@router.post("/reminders", status_code=201)
async def create_reminder(body: ReminderIn, ctx: EditorDep) -> dict[str, Any]:
    r = await cal.add_reminder(
        ctx.session,
        ctx.business,
        title=body.title,
        due_at=body.due_at,
        kind=body.kind,
        notes=body.notes,
        importance=body.importance,
        all_day=body.all_day,
        notify_before_minutes=body.notify_before_minutes,
        repeat=body.repeat,
        actor=Actor.user(ctx.user.id),
        created_by=ctx.user.id,
    )
    return cal.reminder_view(r)


@router.post("/reminders/capture")
async def capture(body: CaptureIn, ctx: EditorDep) -> dict[str, Any]:
    """Paste anything (an email, an invoice, a renewal notice); the model decides what to remember."""
    return await cal.capture_reminders(
        ctx.services, ctx.business_id, body.text, actor=Actor.user(ctx.user.id), created_by=ctx.user.id
    )


@router.patch("/reminders/{reminder_id}")
async def update_reminder(reminder_id: uuid.UUID, body: ReminderPatch, ctx: EditorDep) -> dict[str, Any]:
    r = await _reminder(ctx, reminder_id)
    changes = body.model_dump(exclude_unset=True)
    if "title" in changes:
        r.title = changes["title"].strip()
    if "notes" in changes:
        r.notes = changes["notes"]
    if "importance" in changes:
        r.importance = changes["importance"]
    if "all_day" in changes:
        r.all_day = changes["all_day"]
    if "kind" in changes:
        r.kind = changes["kind"]
    if "repeat" in changes:
        if changes["repeat"] not in REPEATS:
            raise ValidationFailed("Unknown repeat")
        r.repeat = changes["repeat"]
    if "due_at" in changes:
        r.due_at = cal._aware(changes["due_at"], ctx.business.timezone)
    if "notify_before_minutes" in changes:
        r.notify_before_minutes = cal.clean_leads(changes["notify_before_minutes"], r.kind)
    if {"due_at", "notify_before_minutes"} & changes.keys() and r.status == "active":
        r.last_notified_at = None
        r.next_notify_at = next_notify_at(r.due_at, r.notify_before_minutes, last_notified_at=None, now=utcnow())
    return cal.reminder_view(r)


@router.post("/reminders/{reminder_id}/done")
async def done(reminder_id: uuid.UUID, ctx: EditorDep) -> dict[str, Any]:
    r = await _reminder(ctx, reminder_id)
    if r.status == "active":
        await cal.complete_reminder(ctx.session, ctx.business, r, actor=Actor.user(ctx.user.id))
    return cal.reminder_view(r)


@router.post("/reminders/{reminder_id}/snooze")
async def snooze(reminder_id: uuid.UUID, body: SnoozeIn, ctx: EditorDep) -> dict[str, Any]:
    r = await _reminder(ctx, reminder_id)
    if r.status != "active":
        raise ValidationFailed("Only active reminders can be snoozed")
    r.next_notify_at = utcnow() + timedelta(minutes=body.minutes)
    return cal.reminder_view(r)


@router.post("/reminders/{reminder_id}/dismiss")
async def dismiss(reminder_id: uuid.UUID, ctx: EditorDep) -> dict[str, Any]:
    r = await _reminder(ctx, reminder_id)
    r.status, r.next_notify_at = "dismissed", None
    return cal.reminder_view(r)
