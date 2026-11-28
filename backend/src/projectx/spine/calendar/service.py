"""Calendar and reminders: the rules. API handlers and agents both call these functions.

- Scheduling checks the slot against existing events (no double booking unless asked).
- Inviting people outside the business is an action (``spine.calendar.send_invite``): it waits for
  approval by default, and every reschedule or cancellation sends an update with a higher SEQUENCE.
- Every meeting gets a reminder; reminders fire from a periodic job into the notification feed.
- ``capture_reminders`` hands text (an email, a note, a renewal notice) to the model, which decides
  what is worth remembering. Code then checks every date, lead time and repeat before saving.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime, time, timedelta
from email.utils import parseaddr
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.errors import Conflict, NotFound, ValidationFailed
from projectx.kernel.time import utcnow, zone
from projectx.services import Services
from projectx.spine.calendar.ics import build_invite
from projectx.spine.calendar.models import CalendarEvent, CalendarSettings, Reminder
from projectx.spine.calendar.sync import external_busy, queue_push
from projectx.spine.calendar.timing import (
    Interval,
    free_slots,
    next_notify_at,
    overlaps,
    roll_forward,
)
from projectx.spine.decisions import service as decisions
from projectx.spine.decisions.executors import ExecutionContext, ExecutionResult, executor, undoer
from projectx.spine.decisions.models import Decision
from projectx.spine.gateway.contracts import ChatMessage, GatewayError, ModelRequest
from projectx.spine.gateway.offline import offline_generator
from projectx.spine.jobs.registry import periodic
from projectx.spine.ledger.events import EventData, register_event
from projectx.spine.ledger.service import Actor, append
from projectx.spine.mail import Attachment
from projectx.spine.notifications.service import notify
from projectx.spine.policy.actions import ActionType, register_action
from projectx.spine.policy.levels import Level
from projectx.spine.tenancy.models import Business

REMINDER_KINDS = ("meeting", "renewal", "deadline", "payment", "follow_up", "task", "other")
# Sensible lead times when nobody says otherwise (minutes before due).
DEFAULT_LEADS: dict[str, list[int]] = {
    "meeting": [1440, 15],
    "renewal": [43200, 10080, 1440],  # 30 days, 7 days, 1 day: renewals need time to act
    "deadline": [10080, 1440],
    "payment": [4320, 1440],
    "follow_up": [0],
    "task": [0],
    "other": [1440],
}
MAX_LEADS = 5
MAX_LEAD_MINUTES = 366 * 1440
MAX_EVENT = timedelta(days=14)
INVITE = "spine.calendar.send_invite"

# ---------------------------------------------------------------- registry

register_action(
    ActionType(
        key=INVITE,
        module="spine",
        pack="spine.calendar",
        title="Send a calendar invitation",
        description="Email an invitation, update or cancellation (.ics) to the people at a meeting.",
        default_level=Level.ASK_FIRST,
        max_level=Level.RUN_AND_TELL,
        reversible=True,
        undo="Send a cancellation to everyone invited.",
    )
)


class EventChanged(EventData):
    event_id: str
    starts_at: str
    ends_at: str
    attendee_count: int = 0
    source: str = "person"


class ReminderChanged(EventData):
    reminder_id: str
    kind: str
    source: str = "person"


register_event("spine.calendar.scheduled", "An event was put on the calendar", EventChanged)
register_event("spine.calendar.rescheduled", "An event moved to a new time", EventChanged)
register_event("spine.calendar.cancelled", "An event was cancelled", EventChanged)
register_event("spine.reminder.created", "A reminder was stored", ReminderChanged)
register_event("spine.reminder.sent", "A reminder fired", ReminderChanged)
register_event("spine.reminder.completed", "A reminder was marked done", ReminderChanged)


# ---------------------------------------------------------------- settings


async def get_settings(session: AsyncSession, business_id: uuid.UUID) -> CalendarSettings:
    row = await session.get(CalendarSettings, business_id)
    if row is None:
        row = CalendarSettings(business_id=business_id)
        session.add(row)
        await session.flush()
    return row


def settings_view(s: CalendarSettings) -> dict[str, Any]:
    return {
        "working_hours": s.working_hours,
        "buffer_minutes": s.buffer_minutes,
        "min_notice_minutes": s.min_notice_minutes,
        "slot_step_minutes": s.slot_step_minutes,
        "default_duration_minutes": s.default_duration_minutes,
    }


# ---------------------------------------------------------------- events


def _aware(moment: datetime, tz_name: str) -> datetime:
    """Times without an offset are read as the business's local time."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone(tz_name))
    return moment.astimezone(UTC)


def clean_attendees(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for a in raw:
        email = parseaddr(str(a.get("email", "")))[1].strip().lower()
        if "@" not in email or any(c in email for c in '\r\n<>"; ,'):
            raise ValidationFailed(f"Not a valid email address: {a.get('email')!r}")
        if email in seen:
            continue
        seen.add(email)
        name = (str(a.get("name") or "").strip() or None) if a.get("name") else None
        out.append({"email": email, "name": name[:120] if name else None})
    if len(out) > 50:
        raise ValidationFailed("A meeting can invite at most 50 people")
    return out


async def busy_intervals(
    session: AsyncSession, business_id: uuid.UUID, start: datetime, end: datetime, *, exclude: uuid.UUID | None = None
) -> list[Interval]:
    stmt = select(CalendarEvent.starts_at, CalendarEvent.ends_at).where(
        CalendarEvent.business_id == business_id,
        CalendarEvent.status == "scheduled",
        CalendarEvent.starts_at < end,
        CalendarEvent.ends_at > start,
    )
    if exclude is not None:
        stmt = stmt.where(CalendarEvent.id != exclude)
    own = [Interval(a, b) for a, b in (await session.execute(stmt)).all()]
    return own + [Interval(a, b) for a, b in await external_busy(session, start, end)]


async def _check_slot(
    session: AsyncSession,
    business_id: uuid.UUID,
    starts_at: datetime,
    ends_at: datetime,
    *,
    allow_overlap: bool,
    exclude: uuid.UUID | None = None,
) -> None:
    if ends_at <= starts_at:
        raise ValidationFailed("The event must end after it starts")
    if ends_at - starts_at > MAX_EVENT:
        raise ValidationFailed("An event can last at most 14 days")
    if allow_overlap:
        return
    clash = await busy_intervals(session, business_id, starts_at, ends_at, exclude=exclude)
    if any(overlaps(Interval(starts_at, ends_at), c) for c in clash):
        raise Conflict("That time overlaps another event. Pick a free slot, or allow the overlap.", code="slot_taken")


async def schedule_event(
    session: AsyncSession,
    business: Business,
    *,
    title: str,
    starts_at: datetime,
    ends_at: datetime | None = None,
    duration_minutes: int | None = None,
    description: str = "",
    location: str | None = None,
    kind: str = "meeting",
    attendees: list[dict[str, Any]] | None = None,
    contact_id: uuid.UUID | None = None,
    remind_before_minutes: list[int] | None = None,
    allow_overlap: bool = False,
    actor: Actor,
    created_by: uuid.UUID | None = None,
) -> tuple[CalendarEvent, uuid.UUID | None]:
    """Put an event on the calendar. Returns the event and, if people are invited, the decision id."""
    settings = await get_settings(session, business.id)
    start = _aware(starts_at, business.timezone)
    if ends_at is not None:
        end = _aware(ends_at, business.timezone)
    else:
        end = start + timedelta(minutes=duration_minutes or settings.default_duration_minutes)
    await _check_slot(session, business.id, start, end, allow_overlap=allow_overlap)
    people = clean_attendees(attendees or [])
    event = CalendarEvent(
        business_id=business.id,
        title=title.strip()[:200] or "Meeting",
        description=description,
        location=location,
        kind=kind,
        starts_at=start,
        ends_at=end,
        attendees=people,
        contact_id=contact_id,
        source="agent" if actor.type == "agent" else "person",
        created_by=created_by,
    )
    session.add(event)
    await session.flush()
    await _record(session, business.id, "spine.calendar.scheduled", actor, event)
    await add_reminder(
        session,
        business,
        title=event.title,
        due_at=start,
        kind="meeting",
        notify_before_minutes=remind_before_minutes,
        source="calendar",
        event_id=event.id,
        actor=actor,
    )
    decision_id = await _propose_invite(session, business, event, "REQUEST") if people else None
    return event, decision_id


async def reschedule_event(
    session: AsyncSession,
    business: Business,
    event_id: uuid.UUID,
    *,
    starts_at: datetime,
    ends_at: datetime | None = None,
    allow_overlap: bool = False,
    actor: Actor,
) -> tuple[CalendarEvent, uuid.UUID | None]:
    event = await _get_event(session, event_id, lock=True)
    if event.status != "scheduled":
        raise Conflict("This event was cancelled")
    start = _aware(starts_at, business.timezone)
    end = _aware(ends_at, business.timezone) if ends_at else start + (event.ends_at - event.starts_at)
    await _check_slot(session, business.id, start, end, allow_overlap=allow_overlap, exclude=event.id)
    event.starts_at, event.ends_at = start, end
    event.sequence += 1
    await _record(session, business.id, "spine.calendar.rescheduled", actor, event)
    now = utcnow()
    for r in await _event_reminders(session, event.id):
        r.due_at, r.last_notified_at = start, None
        r.next_notify_at = next_notify_at(start, r.notify_before_minutes, last_notified_at=None, now=now)
    await _cancel_pending_invites(session, business.id, event.id)
    decision_id = await _propose_invite(session, business, event, "REQUEST") if event.attendees else None
    return event, decision_id


async def cancel_event(
    session: AsyncSession, business: Business, event_id: uuid.UUID, *, actor: Actor
) -> tuple[CalendarEvent, uuid.UUID | None]:
    event = await _get_event(session, event_id, lock=True)
    if event.status == "cancelled":
        return event, None
    event.status, event.cancelled_at = "cancelled", utcnow()
    event.sequence += 1
    await _record(session, business.id, "spine.calendar.cancelled", actor, event)
    for r in await _event_reminders(session, event.id):
        r.status, r.next_notify_at = "dismissed", None
    await _cancel_pending_invites(session, business.id, event.id)
    # Only people who already got an invitation need to hear it is off.
    decision_id = None
    if event.attendees and event.invite_sequence_sent is not None:
        decision_id = await _propose_invite(session, business, event, "CANCEL")
    return event, decision_id


async def find_free_time(
    session: AsyncSession,
    business: Business,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    duration_minutes: int | None = None,
    limit: int = 20,
    per_day: int | None = None,
) -> list[Interval]:
    s = await get_settings(session, business.id)
    now = utcnow()
    window_start = _aware(start, business.timezone) if start else now
    window_end = _aware(end, business.timezone) if end else window_start + timedelta(days=14)
    if window_end - window_start > timedelta(days=62):
        raise ValidationFailed("Search at most two months at a time")
    buffer = timedelta(minutes=s.buffer_minutes)
    busy = await busy_intervals(session, business.id, window_start - buffer, window_end + buffer)
    return free_slots(
        hours=s.working_hours,
        busy=busy,
        start=window_start,
        end=window_end,
        duration=timedelta(minutes=duration_minutes or s.default_duration_minutes),
        tz_name=business.timezone,
        now=now,
        buffer=buffer,
        min_notice=timedelta(minutes=s.min_notice_minutes),
        step=timedelta(minutes=s.slot_step_minutes),
        limit=limit,
        per_day=per_day,
    )


async def _get_event(session: AsyncSession, event_id: uuid.UUID, *, lock: bool = False) -> CalendarEvent:
    stmt = select(CalendarEvent).where(CalendarEvent.id == event_id)
    if lock:
        stmt = stmt.with_for_update()
    event = await session.scalar(stmt)
    if event is None:
        raise NotFound("Event not found")
    return event


async def _event_reminders(session: AsyncSession, event_id: uuid.UUID) -> list[Reminder]:
    return list(
        (await session.execute(select(Reminder).where(Reminder.event_id == event_id, Reminder.status == "active")))
        .scalars()
        .all()
    )


async def _record(
    session: AsyncSession, business_id: uuid.UUID, type_: str, actor: Actor, event: CalendarEvent
) -> None:
    """Write the change to the ledger and copy it to connected calendars."""
    await append(
        session,
        business_id=business_id,
        type=type_,
        actor=actor,
        subject=("calendar_event", event.id),
        data=_event_data(event),
    )
    await queue_push(session, business_id, event)


def _event_data(e: CalendarEvent) -> dict[str, Any]:
    return {
        "event_id": str(e.id),
        "starts_at": e.starts_at.isoformat(),
        "ends_at": e.ends_at.isoformat(),
        "attendee_count": len(e.attendees),
        "source": e.source,
    }


def _local_label(moment: datetime, tz_name: str) -> str:
    local = moment.astimezone(zone(tz_name))
    return f"{local:%a %d %b, %H:%M} ({tz_name})"


async def _propose_invite(session: AsyncSession, business: Business, event: CalendarEvent, method: str) -> uuid.UUID:
    who = ", ".join(a.get("name") or a["email"] for a in event.attendees[:5])
    more = f" and {len(event.attendees) - 5} more" if len(event.attendees) > 5 else ""
    when = _local_label(event.starts_at, business.timezone)
    if method == "CANCEL":
        title, summary = f"Tell {who}{more} that “{event.title}” is cancelled", f"Was {when}."
    elif event.invite_sequence_sent is not None:
        title, summary = f"Send the new time for “{event.title}” to {who}{more}", f"Now {when}."
    else:
        title, summary = f"Invite {who}{more} to “{event.title}”", f"{when}, {_minutes(event)} minutes."
    outcome = await decisions.propose(
        session,
        business.id,
        decisions.Proposal(
            action_type=INVITE,
            title=title,
            summary=summary,
            payload={"event_id": str(event.id), "method": method, "sequence": event.sequence},
            preview={
                "event": {
                    "title": event.title,
                    "starts_at": event.starts_at.isoformat(),
                    "ends_at": event.ends_at.isoformat(),
                    "location": event.location,
                    "attendees": event.attendees,
                },
                "method": method,
            },
            why_now="The calendar changed and the people invited don't know yet.",
            if_no="The event stays on your calendar; nobody is emailed.",
            subject=("calendar_event", event.id),
            requested_by="pack:spine.calendar",
            priority=30,
            expires_at=event.starts_at,
        ),
    )
    assert outcome.decision_id is not None  # the action is registered, so it is never blocked
    return outcome.decision_id


def _minutes(e: CalendarEvent) -> int:
    return int((e.ends_at - e.starts_at).total_seconds() // 60)


async def _cancel_pending_invites(session: AsyncSession, business_id: uuid.UUID, event_id: uuid.UUID) -> None:
    """An older invitation that was never sent is out of date: retire it so only the latest goes out."""
    stale = (
        await session.execute(
            select(Decision.id).where(
                Decision.action_type == INVITE,
                Decision.status == "pending",
                Decision.subject_type == "calendar_event",
                Decision.subject_id == event_id,
            )
        )
    ).scalars()
    for decision_id in list(stale):
        await decisions.cancel(session, business_id, decision_id, reason="Replaced by a newer change to the event.")


async def _send_invites(ctx: ExecutionContext, method: str) -> dict[str, Any]:
    services = ctx.services
    async with services.db.tenant(ctx.business_id) as session:
        event = await _get_event(session, uuid.UUID(ctx.payload["event_id"]))
        business = await session.get(Business, ctx.business_id)
        assert business is not None
        if method == "REQUEST" and event.status == "cancelled":
            return {"sent": 0, "skipped": "The event was cancelled before the invitation went out."}
        session.expunge(event)  # a detached copy: no transaction stays open while mail is sent
        snap = event
        biz_name, tz_name = business.name, business.timezone
    organizer_name, organizer_email = parseaddr(services.settings.email_from)
    ics = build_invite(
        uid=f"{ctx.payload['event_id']}@projectx",
        method=method,
        sequence=snap.sequence,
        title=snap.title,
        description=snap.description,
        location=snap.location,
        starts_at=snap.starts_at,
        ends_at=snap.ends_at,
        organizer_email=organizer_email,
        organizer_name=biz_name or organizer_name,
        attendees=snap.attendees,
        now=utcnow(),
    )
    when = _local_label(snap.starts_at, tz_name)
    verb = "Cancelled" if method == "CANCEL" else ("Updated" if snap.sequence else "Invitation")
    subject = f"{verb}: {snap.title} · {when}"
    text = f"{snap.title}\n{when}\n" + (f"{snap.location}\n" if snap.location else "")
    if method == "CANCEL":
        text = f"This event is cancelled.\n\n{text}"
    html = "<p>" + "<br>".join(_html_escape(line) for line in text.strip().split("\n")) + "</p>"
    sent = 0
    for a in snap.attendees:
        await services.mailer.send(
            to=a["email"],
            subject=subject,
            html=html,
            text=text,
            attachments=[Attachment("invite.ics", "text/calendar", ics, {"method": method})],
        )
        sent += 1
    async with services.db.tenant(ctx.business_id) as session:
        event = await _get_event(session, uuid.UUID(ctx.payload["event_id"]), lock=True)
        event.invite_sequence_sent = snap.sequence
    return {"sent": sent, "method": method, "sequence": snap.sequence}


def _html_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@executor(INVITE)
async def _execute_invite(ctx: ExecutionContext) -> ExecutionResult:
    method = ctx.payload.get("method", "REQUEST")
    result = await _send_invites(ctx, method)
    undo = {"event_id": ctx.payload["event_id"]} if method == "REQUEST" and result.get("sent") else None
    return ExecutionResult(data=result, undo_payload=undo, summary=f"Emailed {result.get('sent', 0)} people.")


@undoer(INVITE)
async def _undo_invite(ctx: ExecutionContext) -> dict[str, Any]:
    async with ctx.services.db.tenant(ctx.business_id) as session:
        event = await _get_event(session, uuid.UUID(ctx.payload["event_id"]), lock=True)
        if event.status != "cancelled":
            event.status, event.cancelled_at = "cancelled", utcnow()
            event.sequence += 1
            await _record(session, ctx.business_id, "spine.calendar.cancelled", Actor.system("undo"), event)
            for r in await _event_reminders(session, event.id):
                r.status, r.next_notify_at = "dismissed", None
    return await _send_invites(ctx, "CANCEL")


def event_view(e: CalendarEvent) -> dict[str, Any]:
    return {
        "id": str(e.id),
        "title": e.title,
        "description": e.description,
        "location": e.location,
        "kind": e.kind,
        "starts_at": e.starts_at.isoformat(),
        "ends_at": e.ends_at.isoformat(),
        "status": e.status,
        "attendees": e.attendees,
        "contact_id": str(e.contact_id) if e.contact_id else None,
        "sequence": e.sequence,
        "invited": e.invite_sequence_sent is not None,
        "invite_up_to_date": e.invite_sequence_sent == e.sequence,
        "source": e.source,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


# ---------------------------------------------------------------- reminders


def clean_leads(values: list[int] | None, kind: str) -> list[int]:
    if not values:
        return list(DEFAULT_LEADS.get(kind, DEFAULT_LEADS["other"]))
    leads = sorted({int(v) for v in values if 0 <= int(v) <= MAX_LEAD_MINUTES}, reverse=True)
    if not leads:
        raise ValidationFailed("Reminder times must be between 0 minutes and a year before")
    return leads[:MAX_LEADS]


async def add_reminder(
    session: AsyncSession,
    business: Business,
    *,
    title: str,
    due_at: datetime,
    kind: str = "other",
    notes: str = "",
    importance: str = "normal",
    all_day: bool = False,
    notify_before_minutes: list[int] | None = None,
    repeat: str = "none",
    source: str = "person",
    event_id: uuid.UUID | None = None,
    dedupe_key: str | None = None,
    actor: Actor,
    created_by: uuid.UUID | None = None,
) -> Reminder:
    if kind not in REMINDER_KINDS:
        kind = "other"
    due = _aware(due_at, business.timezone)
    leads = clean_leads(notify_before_minutes, kind)
    reminder = Reminder(
        business_id=business.id,
        title=title.strip()[:200] or "Reminder",
        notes=notes[:4000],
        kind=kind,
        importance=importance if importance in ("low", "normal", "high") else "normal",
        due_at=due,
        all_day=all_day,
        notify_before_minutes=leads,
        repeat=repeat,
        next_notify_at=next_notify_at(due, leads, last_notified_at=None, now=utcnow()),
        source=source,
        event_id=event_id,
        dedupe_key=dedupe_key,
        created_by=created_by,
    )
    session.add(reminder)
    await session.flush()
    await append(
        session,
        business_id=business.id,
        type="spine.reminder.created",
        actor=actor,
        subject=("reminder", reminder.id),
        data={"reminder_id": str(reminder.id), "kind": kind, "source": source},
    )
    return reminder


async def complete_reminder(session: AsyncSession, business: Business, reminder: Reminder, *, actor: Actor) -> None:
    """Done. A repeating reminder moves on to its next occurrence instead of closing."""
    now = utcnow()
    await append(
        session,
        business_id=business.id,
        type="spine.reminder.completed",
        actor=actor,
        subject=("reminder", reminder.id),
        data={"reminder_id": str(reminder.id), "kind": reminder.kind, "source": reminder.source},
    )
    if reminder.repeat != "none":
        due = roll_forward(reminder.due_at, reminder.repeat, business.timezone, max(now, reminder.due_at))
        reminder.due_at, reminder.last_notified_at = due, None
        reminder.next_notify_at = next_notify_at(due, reminder.notify_before_minutes, last_notified_at=None, now=now)
        return
    reminder.status, reminder.completed_at, reminder.next_notify_at = "done", now, None


def reminder_view(r: Reminder) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "title": r.title,
        "notes": r.notes,
        "kind": r.kind,
        "importance": r.importance,
        "due_at": r.due_at.isoformat(),
        "all_day": r.all_day,
        "notify_before_minutes": r.notify_before_minutes,
        "repeat": r.repeat,
        "next_notify_at": r.next_notify_at.isoformat() if r.next_notify_at else None,
        "last_notified_at": r.last_notified_at.isoformat() if r.last_notified_at else None,
        "status": r.status,
        "source": r.source,
        "event_id": str(r.event_id) if r.event_id else None,
        "overdue": r.status == "active" and r.due_at <= utcnow(),
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@periodic("spine.send_reminders", every=timedelta(minutes=1))
async def send_due_reminders(services: Services, now: datetime) -> None:
    async with services.db.session() as session:
        businesses = list((await session.execute(select(Business).where(Business.status == "active"))).scalars())
    for business in businesses:
        async with services.db.tenant(business.id) as session:
            due = list(
                (
                    await session.execute(
                        select(Reminder)
                        .where(Reminder.status == "active", Reminder.next_notify_at <= now)
                        .order_by(Reminder.next_notify_at)
                        .limit(200)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            for r in due:
                await _fire(session, business, r, now)


async def _fire(session: AsyncSession, business: Business, r: Reminder, now: datetime) -> None:
    left = r.due_at - now
    if left <= timedelta(0):
        when = "is due now" if left > -timedelta(minutes=5) else "is overdue"
    elif r.all_day:
        days = r.due_at.astimezone(zone(business.timezone)).date() - now.astimezone(zone(business.timezone)).date()
        when = "is due today" if days.days == 0 else f"is due in {_plural(days.days, 'day')}"
    else:
        when = f"is in {_human(left)}"
    await notify(
        session,
        business.id,
        kind="reminder",
        level="warning" if r.importance == "high" or left <= timedelta(0) else "info",
        title=f"{r.title} {when}"[:200],
        body=f"{_local_label(r.due_at, business.timezone)}" + (f"\n{r.notes}" if r.notes else ""),
        link=f"/reminders/{r.id}",
    )
    await append(
        session,
        business_id=business.id,
        type="spine.reminder.sent",
        actor=Actor.system("reminders"),
        subject=("reminder", r.id),
        data={"reminder_id": str(r.id), "kind": r.kind, "source": r.source},
    )
    r.last_notified_at = now
    if now >= r.due_at and r.repeat != "none":
        r.due_at = roll_forward(r.due_at, r.repeat, business.timezone, now)
        r.last_notified_at = None
        r.next_notify_at = next_notify_at(r.due_at, r.notify_before_minutes, last_notified_at=None, now=now)
        return
    r.next_notify_at = next_notify_at(r.due_at, r.notify_before_minutes, last_notified_at=now, now=now)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _human(delta: timedelta) -> str:
    minutes = round(delta.total_seconds() / 60)
    if minutes < 60:
        return _plural(max(minutes, 1), "minute")
    hours = round(minutes / 60)
    if hours < 36:
        return _plural(hours, "hour")
    return _plural(round(hours / 24), "day")


# ---------------------------------------------------------------- the model decides what to remember


class ReminderDraft(BaseModel):
    title: str = Field(description="Short and specific, e.g. 'Renew bought.news domain'", max_length=200)
    kind: Literal["meeting", "renewal", "deadline", "payment", "follow_up", "task", "other"]
    due: str = Field(description="When it is due, copied from the text: YYYY-MM-DD or YYYY-MM-DDTHH:MM (local time)")
    all_day: bool = Field(description="True when the text gives a date but no time")
    notify_before_minutes: list[int] = Field(
        description="When to warn, in minutes before due. Leave empty to use sensible defaults for the kind."
    )
    repeat: Literal["none", "daily", "weekly", "monthly", "yearly"]
    importance: Literal["low", "normal", "high"]
    notes: str = Field(description="One line of context a person needs when the reminder fires", max_length=500)
    evidence: str = Field(description="The exact words in the text that give the date", max_length=300)


class ReminderExtraction(BaseModel):
    reminders: list[ReminderDraft] = Field(description="Empty when there is nothing with a date worth remembering")


_SYSTEM = """You read text a business received or wrote (emails, invoices, renewal notices, notes, chat) and decide
what is worth a reminder: meetings, renewals (domains, hosting, licences, subscriptions, insurance), deadlines,
payments due, promised follow-ups. Today is {today} ({weekday}) in the business time zone {tz}.

Rules:
- Only things with a date or time you can point to in the text. Never guess a date. Put the words in `evidence`.
- Resolve relative dates ("next Tuesday", "in 3 days") against today and write the result as a date.
- Skip marketing, newsletters, past events, and anything already finished.
- Pick lead times a careful assistant would: renewals and contracts weeks ahead, payments days ahead, meetings a
  day and minutes ahead. Leave the list empty if unsure.
- A yearly renewal repeats yearly; most other things don't repeat.
- No more than 10 reminders. Titles are short and specific. Never copy passwords, card numbers or codes."""


async def capture_reminders(
    services: Services,
    business_id: uuid.UUID,
    text: str,
    *,
    actor: Actor,
    created_by: uuid.UUID | None = None,
    source: str = "agent",
) -> dict[str, Any]:
    """Let the model read `text` and store what deserves a reminder. Returns what was kept and skipped."""
    async with services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        assert business is not None
        tz_name = business.timezone
    tz = zone(tz_name)
    today = utcnow().astimezone(tz).date()
    try:
        result = await services.gateway.run(
            business_id,
            ModelRequest(
                task="spine.reminders.extract",
                system=_SYSTEM.format(today=today.isoformat(), weekday=f"{today:%A}", tz=tz_name),
                messages=[ChatMessage("user", text[:12_000])],
                output_schema=ReminderExtraction,
                max_tokens=1500,
                rung="small",
                context={"text": text[:12_000], "today": today.isoformat()},
                verifier=_verify_extraction,
            ),
        )
    except GatewayError as exc:
        raise ValidationFailed(f"The model could not read this right now: {exc}", code="model_unavailable") from exc
    extraction = result.parsed_as(ReminderExtraction)

    created: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    now = utcnow()
    async with services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        assert business is not None
        for draft in extraction.reminders[:10]:
            due, all_day, problem = _parse_due(draft.due, tz_name, draft.all_day)
            if problem or due is None:
                skipped.append({"title": draft.title, "reason": problem or "No date"})
                continue
            if due <= now and draft.repeat == "none":
                skipped.append({"title": draft.title, "reason": "Already in the past"})
                continue
            if draft.evidence and not _evidence_in(draft.evidence, text):
                skipped.append({"title": draft.title, "reason": "The date isn't in the text"})
                continue
            if due <= now:
                due = roll_forward(due, draft.repeat, tz_name, now)
            key = _dedupe_key(draft.kind, draft.title, due.astimezone(tz).date())
            exists = await session.scalar(select(Reminder.id).where(Reminder.dedupe_key == key))
            if exists is not None:
                skipped.append({"title": draft.title, "reason": "Already stored"})
                continue
            try:
                leads = clean_leads(draft.notify_before_minutes, draft.kind)
            except ValidationFailed:
                leads = clean_leads(None, draft.kind)
            reminder = await add_reminder(
                session,
                business,
                title=draft.title,
                due_at=due,
                kind=draft.kind,
                notes=draft.notes,
                importance=draft.importance,
                all_day=all_day,
                notify_before_minutes=leads,
                repeat=draft.repeat,
                source=source,
                dedupe_key=key,
                actor=actor,
                created_by=created_by,
            )
            created.append(reminder_view(reminder))
    return {"created": created, "skipped": skipped, "model": result.model, "cost_micro_usd": result.cost_micro_usd}


def _verify_extraction(result: Any) -> str | None:
    parsed = result.parsed
    if not isinstance(parsed, ReminderExtraction):
        return "The answer did not match the schema."
    for d in parsed.reminders:
        if _parse_due(d.due, "UTC", d.all_day)[2]:
            return f"'{d.due}' is not a date in YYYY-MM-DD or YYYY-MM-DDTHH:MM form."
    return None


_DUE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2})(?::\d{2})?)?$")
ALL_DAY_AT = time(9, 0)  # an all-day item is "due" at 09:00 local, so warnings arrive in working hours


def _parse_due(value: str, tz_name: str, all_day_hint: bool) -> tuple[datetime | None, bool, str | None]:
    m = _DUE.match(value.strip())
    if not m:
        return None, False, f"Not a date: {value!r}"
    try:
        day = date.fromisoformat(m.group(1))
        at = time.fromisoformat(m.group(2)) if m.group(2) else None
    except ValueError:
        return None, False, f"Not a real date: {value!r}"
    if day.year > utcnow().year + 20:
        return None, False, "Too far in the future"
    all_day = at is None or (all_day_hint and at == time(0))
    local = datetime.combine(day, ALL_DAY_AT if all_day else at or ALL_DAY_AT, tzinfo=zone(tz_name))
    return local.astimezone(UTC), all_day, None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _evidence_in(evidence: str, text: str) -> bool:
    """The quoted words must really be in the text (spacing and punctuation aside)."""
    return _norm(evidence) in _norm(text)


def _dedupe_key(kind: str, title: str, day: date) -> str:
    return f"{kind}:{day.isoformat()}:{_norm(title)[:80]}"


# Offline (no model key): find ISO or written dates in the text so the feature works end to end.
_ISO_IN_TEXT = re.compile(r"\b(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}))?\b")
_KIND_WORDS = (
    ("renewal", ("renew", "expire", "expiry", "expires", "subscription", "domain", "licence", "license")),
    ("payment", ("invoice", "payment", "pay ", "due amount", "bill")),
    ("meeting", ("meeting", "call", "demo", "interview", "catch up", "catch-up")),
    ("deadline", ("deadline", "submit", "filing", "due by")),
    ("follow_up", ("follow up", "follow-up", "get back")),
)


@offline_generator("spine.reminders.extract")
def _offline_extract(req: ModelRequest) -> dict[str, Any]:
    text = str(req.context.get("text", ""))
    out: list[dict[str, Any]] = []
    for line in re.split(r"(?<=[.!?\n])\s+", text):
        m = _ISO_IN_TEXT.search(line)
        if not m:
            continue
        lower = line.lower()
        kind = next((k for k, words in _KIND_WORDS if any(w in lower for w in words)), "other")
        title = re.sub(r"\s+", " ", line.replace(m.group(0), "")).strip(" .,:;-")[:120] or "Reminder"
        out.append(
            {
                "title": title,
                "kind": kind,
                "due": f"{m.group(1)}T{m.group(2)}" if m.group(2) else m.group(1),
                "all_day": not m.group(2),
                "notify_before_minutes": [],
                "repeat": "yearly" if kind == "renewal" and "year" in lower else "none",
                "importance": "high" if kind in ("renewal", "payment") else "normal",
                "notes": "",
                "evidence": m.group(0),
            }
        )
    return {"reminders": out[:10]}
