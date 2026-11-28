"""The assistant stays on: it wakes up by itself, looks at the person's day, and speaks only when it matters.

When it wakes:
- a morning brief at the time each person picks (working days by default),
- 30 minutes before each of today's meetings, a midday check and an end-of-day wrap,
- straight away when something changes underneath (a connected calendar now clashes with a meeting).

What it says is decided by code from records: clashes, meetings whose invitation is still unapproved,
reminders and promises due, decisions waiting. Each signal is said once (the `told` log), quiet hours
are kept, and nudges are capped per day. A quiet check never calls a model, which keeps an always-on
assistant affordable. When there is something to say, an open model may smooth the wording, but every
time and number in its text must already be in the facts, or the plain version is sent instead.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.time import utcnow, zone
from projectx.services import Services
from projectx.spine.calendar.models import CalendarBusy, CalendarEvent, Reminder
from projectx.spine.calendar.timing import WEEKDAYS
from projectx.spine.context.models import AgentWakeup, AssistantSettings, ContextEntry
from projectx.spine.decisions.models import Decision
from projectx.spine.gateway.contracts import ChatMessage, GatewayError, ModelRequest, ModelResult
from projectx.spine.gateway.offline import offline_generator
from projectx.spine.identity.models import User
from projectx.spine.jobs.queue import enqueue
from projectx.spine.jobs.registry import JobContext, job, periodic
from projectx.spine.ledger.events import EventData, register_event
from projectx.spine.ledger.service import Actor, append
from projectx.spine.notifications.service import notify
from projectx.spine.tenancy.models import Business, Membership

PRIORITY = {"high": 3, "medium": 2, "low": 1, "info": 0}
TOLD_KEEP = timedelta(days=4)


class Woke(EventData):
    wakeup_id: str
    kind: str
    outcome: str  # delivered | quiet | skipped
    signals: int


register_event("spine.assistant.woke", "The assistant woke up on its own and looked at someone's day", Woke)


@dataclass(frozen=True)
class Signal:
    key: str  # stable, so the same thing is said once
    priority: str  # high | medium | low | info
    line: str


# ---------------------------------------------------------------- settings and delivery


async def get_settings(session: AsyncSession, business_id: uuid.UUID, user_id: uuid.UUID) -> AssistantSettings:
    row = await session.get(AssistantSettings, (business_id, user_id))
    if row is None:
        row = AssistantSettings(business_id=business_id, user_id=user_id)
        session.add(row)
        await session.flush()
    return row


def settings_view(s: AssistantSettings) -> dict[str, Any]:
    return {
        "enabled": s.enabled,
        "brief_time": s.brief_time,
        "brief_days": s.brief_days,
        "quiet_start": s.quiet_start,
        "quiet_end": s.quiet_end,
        "channels": s.channels,
        "max_nudges_per_day": s.max_nudges_per_day,
    }


Deliverer = Callable[[Services, uuid.UUID, uuid.UUID, str, str], Awaitable[None]]
_DELIVERERS: dict[str, Deliverer] = {}


def register_deliverer(channel: str, fn: Deliverer) -> None:
    """Modules add channels here (WhatsApp, SMS…). `app` and `email` are built in."""
    _DELIVERERS[channel] = fn


async def _deliver_email(services: Services, business_id: uuid.UUID, user_id: uuid.UUID, title: str, body: str) -> None:
    async with services.db.session() as session:
        user = await session.get(User, user_id)
        to = user.email if user and user.is_active else None
    if to:
        html = "<p>" + "<br>".join(_esc(line) for line in body.split("\n")) + "</p>"
        await services.mailer.send(to=to, subject=title, html=html, text=body)


def _esc(v: str) -> str:
    return v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


register_deliverer("email", _deliver_email)


# ---------------------------------------------------------------- the situation, computed from records


def _hm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def in_quiet_hours(s: AssistantSettings, local: datetime) -> bool:
    start, end, now = _hm(s.quiet_start), _hm(s.quiet_end), local.time()
    return (start <= now or now < end) if start > end else (start <= now < end)


async def situation(
    session: AsyncSession, business: Business, user_id: uuid.UUID, now: datetime, *, brief: bool
) -> list[Signal]:
    tz = zone(business.timezone)
    local = now.astimezone(tz)
    day_start = datetime.combine(local.date(), time(0), tzinfo=tz).astimezone(UTC)
    day_end = day_start + timedelta(days=1)
    horizon = now + timedelta(hours=48)

    def at(moment: datetime) -> str:
        m = moment.astimezone(tz)
        return f"{m:%H:%M}" if m.date() == local.date() else f"{m:%a %H:%M}"

    signals: list[Signal] = []
    events = list(
        (
            await session.execute(
                select(CalendarEvent)
                .where(
                    CalendarEvent.status == "scheduled",
                    CalendarEvent.ends_at > now,
                    CalendarEvent.starts_at < horizon,
                )
                .order_by(CalendarEvent.starts_at)
            )
        ).scalars()
    )
    busy = (
        await session.execute(
            select(CalendarBusy.starts_at, CalendarBusy.ends_at).where(
                CalendarBusy.starts_at < horizon, CalendarBusy.ends_at > now
            )
        )
    ).all()
    for i, e in enumerate(events):
        other = next((o for o in events[i + 1 :] if o.starts_at < e.ends_at and e.starts_at < o.ends_at), None)
        outside = any(a < e.ends_at and e.starts_at < b for a, b in busy)
        if other is not None:
            signals.append(
                Signal(
                    f"clash:{e.id}:{e.sequence}:{other.id}",
                    "high",
                    f"Clash at {at(e.starts_at)}: “{e.title}” overlaps “{other.title}”.",
                )
            )
        elif outside:
            signals.append(
                Signal(
                    f"clash:{e.id}:{e.sequence}:ext",
                    "high",
                    f"Clash at {at(e.starts_at)}: “{e.title}” overlaps something on your connected calendar.",
                )
            )
    unsent = (
        await session.execute(
            select(Decision, CalendarEvent)
            .join(CalendarEvent, CalendarEvent.id == Decision.subject_id)
            .where(
                Decision.action_type == "spine.calendar.send_invite",
                Decision.status == "pending",
                CalendarEvent.status == "scheduled",
                CalendarEvent.starts_at < now + timedelta(hours=24),
                CalendarEvent.starts_at > now,
            )
        )
    ).all()
    for d, e in unsent:
        signals.append(
            Signal(
                f"invite:{d.id}",
                "high",
                f"“{e.title}” is at {at(e.starts_at)} but the invitation hasn't gone out. Approve it so they know.",
            )
        )
    soon = [e for e in events if now < e.starts_at <= now + timedelta(minutes=45) and e.attendees]
    for e in soon:
        who = ", ".join(a.get("name") or a["email"] for a in e.attendees[:3])
        signals.append(Signal(f"prep:{e.id}:{e.sequence}", "medium", f"At {at(e.starts_at)}: “{e.title}” with {who}."))
    reminders = (
        await session.execute(
            select(Reminder)
            .where(Reminder.status == "active", Reminder.due_at < day_end)
            .order_by(Reminder.due_at)
            .limit(10)
        )
    ).scalars()
    for r in reminders:
        overdue = r.due_at <= now
        signals.append(
            Signal(
                f"reminder:{r.id}:{r.due_at.date().isoformat()}:{'late' if overdue else 'today'}",
                "medium" if overdue or r.importance == "high" else "low",
                f"{'Overdue' if overdue else 'Due ' + at(r.due_at)}: {r.title}.",
            )
        )
    promises = (
        await session.execute(
            select(ContextEntry).where(
                ContextEntry.kind == "commitment",
                ContextEntry.status == "active",
                ContextEntry.user_id == user_id,
            )
        )
    ).scalars()
    for p in promises:
        due = p.data.get("due_at")
        if due and datetime.fromisoformat(due) <= horizon:
            signals.append(
                Signal(f"promise:{p.id}", "medium", f"You promised: {p.value} (due {at(datetime.fromisoformat(due))}).")
            )
    waiting = await session.scalar(
        select(func.count())
        .select_from(Decision)
        .where(Decision.status == "pending", Decision.kind.in_(("approval", "choice")))
    )
    if waiting:
        signals.append(
            Signal(
                f"decisions:{local.date().isoformat()}:{waiting}",
                "low",
                f"{waiting} decision{'s' if waiting != 1 else ''} waiting for you.",
            )
        )
    if brief:
        today = [e for e in events if day_start <= e.starts_at < day_end]
        if today:
            listed = "; ".join(f"{at(e.starts_at)} {e.title}" for e in today[:6])
            more = f" and {len(today) - 6} more" if len(today) > 6 else ""
            count = f"{len(today)} meeting{'s' if len(today) != 1 else ''}"
            signals.append(Signal(f"day:{local.date().isoformat()}", "info", f"Today, {count}: {listed}{more}."))
        else:
            signals.append(Signal(f"day:{local.date().isoformat()}", "info", "No meetings today."))
    return signals


def compose(signals: list[Signal], *, brief: bool, first_name: str) -> tuple[str, str]:
    """Front-load what matters: the title and first line carry the most important thing."""
    ordered = sorted(signals, key=lambda s: -PRIORITY[s.priority])
    top = ordered[0]
    title = f"Good morning{', ' + first_name if first_name else ''}: " + top.line if brief else top.line
    body = "\n".join(s.line for s in ordered)
    return title[:200], body


# ---------------------------------------------------------------- optional polish by an open model, checked by code


class Wording(BaseModel):
    text: str = Field(description="The same facts as a short, warm message. Most important first. No new facts")


def _facts_kept(source: str, written: str) -> bool:
    """Every time and number in the polished text must already be in the facts."""
    tokens = set(re.findall(r"\d{1,2}:\d{2}|\d+", written))
    return tokens <= set(re.findall(r"\d{1,2}:\d{2}|\d+", source)) and len(written) <= len(source) * 2 + 200


async def polish(services: Services, business_id: uuid.UUID, body: str) -> str:
    def verify(result: ModelResult) -> str | None:
        parsed = result.parsed
        if not isinstance(parsed, Wording) or not _facts_kept(body, parsed.text):
            return "Keep exactly the same facts, times and numbers."
        return None

    try:
        result = await services.gateway.run(
            business_id,
            ModelRequest(
                task="spine.proactive.compose",
                system="You write short messages from a trusted executive assistant. Plain, warm, no fluff. Keep every "
                "fact, time and number exactly; add nothing. Put the most important thing first.",
                messages=[ChatMessage("user", body)],
                output_schema=Wording,
                max_tokens=600,
                rung="small",
                max_rung="small",
                context={"text": body},
                verifier=verify,
            ),
        )
    except GatewayError:
        return body
    wording = result.parsed
    if isinstance(wording, Wording) and _facts_kept(body, wording.text):
        return wording.text
    return body


@offline_generator("spine.proactive.compose")
def _offline_compose(req: ModelRequest) -> dict[str, Any]:
    return {"text": str(req.context.get("text", ""))}


# ---------------------------------------------------------------- waking up


async def wake_soon(
    session: AsyncSession,
    business_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    reason: str,
    key: str,
    at: datetime | None = None,
) -> None:
    """Something changed: look again shortly. Safe to call often; the key keeps one wakeup per cause."""
    exists = await session.scalar(select(AgentWakeup.id).where(AgentWakeup.dedupe_key == key))
    if exists is None:
        session.add(
            AgentWakeup(
                business_id=business_id,
                user_id=user_id,
                wake_at=at or utcnow(),
                kind="check",
                reason=reason[:300],
                dedupe_key=key[:160],
            )
        )
        await session.flush()


async def plan_wakeups(session: AsyncSession, business: Business, user_id: uuid.UUID, now: datetime) -> None:
    s = await get_settings(session, business.id, user_id)
    if not s.enabled:
        return
    tz = zone(business.timezone)
    local = now.astimezone(tz)
    for offset in range(0, 8):
        day: date = local.date() + timedelta(days=offset)
        if WEEKDAYS[day.weekday()] not in s.brief_days:
            continue
        at = datetime.combine(day, _hm(s.brief_time), tzinfo=tz).astimezone(UTC)
        if at > now - timedelta(minutes=5):
            await _ensure(session, business.id, user_id, at, "brief", "Morning brief", f"brief:{user_id}:{day}")
            break
    day_end = datetime.combine(local.date(), time(23, 59), tzinfo=tz).astimezone(UTC)
    meetings = (
        await session.execute(
            select(CalendarEvent).where(
                CalendarEvent.status == "scheduled",
                CalendarEvent.starts_at > now + timedelta(minutes=20),
                CalendarEvent.starts_at < day_end,
            )
        )
    ).scalars()
    for e in meetings:
        await _ensure(
            session,
            business.id,
            user_id,
            e.starts_at - timedelta(minutes=30),
            "check",
            f"Before “{e.title[:200]}”",
            f"check:{user_id}:{e.id}:{e.sequence}",
        )
    if WEEKDAYS[local.weekday()] in s.brief_days:
        for label, hhmm in (("Midday check", "12:30"), ("End of day", "17:30")):
            at = datetime.combine(local.date(), _hm(hhmm), tzinfo=tz).astimezone(UTC)
            if at > now:
                await _ensure(session, business.id, user_id, at, "check", label, f"{label}:{user_id}:{local.date()}")


async def _ensure(
    session: AsyncSession, business_id: uuid.UUID, user_id: uuid.UUID, at: datetime, kind: str, reason: str, key: str
) -> None:
    exists = await session.scalar(select(AgentWakeup.id).where(AgentWakeup.dedupe_key == key))
    if exists is None:
        session.add(
            AgentWakeup(business_id=business_id, user_id=user_id, wake_at=at, kind=kind, reason=reason, dedupe_key=key)
        )
        await session.flush()


@periodic("spine.assistant.plan_wakeups", every=timedelta(minutes=15))
async def plan_all(services: Services, now: datetime) -> None:
    async with services.db.session() as session:
        members = (
            await session.execute(
                select(Membership.business_id, Membership.user_id)
                .join(Business, Business.id == Membership.business_id)
                .where(Business.status == "active")
            )
        ).all()
    by_business: dict[uuid.UUID, list[uuid.UUID]] = {}
    for business_id, user_id in members:
        by_business.setdefault(business_id, []).append(user_id)
    for business_id, users in by_business.items():
        async with services.db.tenant(business_id) as session:
            business = await session.get(Business, business_id)
            if business is None:
                continue
            for user_id in users:
                await plan_wakeups(session, business, user_id, now)


@periodic("spine.assistant.wake_due", every=timedelta(minutes=1))
async def wake_due(services: Services, now: datetime) -> None:
    async with services.db.session() as session:
        business_ids = list((await session.execute(select(Business.id).where(Business.status == "active"))).scalars())
    for business_id in business_ids:
        async with services.db.tenant(business_id) as session:
            due = (
                await session.execute(
                    select(AgentWakeup.id).where(AgentWakeup.status == "pending", AgentWakeup.wake_at <= now).limit(100)
                )
            ).scalars()
            for wakeup_id in list(due):
                await enqueue(
                    session,
                    "spine.assistant.wake",
                    {"wakeup_id": str(wakeup_id)},
                    business_id=business_id,
                    idempotency_key=f"wake:{wakeup_id}",
                )


@job("spine.assistant.wake", max_attempts=4, timeout_s=120)
async def wake(ctx: JobContext) -> dict[str, Any]:
    business_id = ctx.require_business()
    return await run_wakeup(ctx.services, business_id, uuid.UUID(ctx.payload["wakeup_id"]))


async def run_wakeup(services: Services, business_id: uuid.UUID, wakeup_id: uuid.UUID) -> dict[str, Any]:
    now = utcnow()
    async with services.db.tenant(business_id) as session:
        w = await session.get(AgentWakeup, wakeup_id, with_for_update=True)
        business = await session.get(Business, business_id)
        if w is None or business is None or w.status != "pending":
            return {"skipped": "not pending"}
        s = await get_settings(session, business_id, w.user_id)
        user = await session.get(User, w.user_id)
        first = user.name.split()[0] if user and user.name else ""
        local = now.astimezone(zone(business.timezone))
        brief = w.kind == "brief"
        outcome, signals_out, title, body = "quiet", [], "", ""
        if not s.enabled or (not brief and in_quiet_hours(s, local)):
            outcome = "skipped"
        else:
            signals = await situation(session, business, w.user_id, now, brief=brief)
            told = {k: v for k, v in (s.told or {}).items() if datetime.fromisoformat(v) > now - TOLD_KEEP}
            fresh = [x for x in signals if x.key not in told]
            worth = fresh if brief else [x for x in fresh if PRIORITY[x.priority] >= PRIORITY["medium"]]
            nudges_today = await session.scalar(
                select(func.count())
                .select_from(AgentWakeup)
                .where(
                    AgentWakeup.user_id == w.user_id,
                    AgentWakeup.kind == "check",
                    AgentWakeup.status == "done",
                    AgentWakeup.done_at >= now - timedelta(hours=24),
                    AgentWakeup.result["outcome"].astext == "delivered",
                )
            )
            if not brief and (nudges_today or 0) >= s.max_nudges_per_day:
                outcome = "skipped"
            elif worth:
                title, body = compose(worth, brief=brief, first_name=first)
                signals_out = worth
                for x in worth:
                    told[x.key] = now.isoformat()
                outcome = "delivered"
            s.told = told
        w.status, w.done_at = ("skipped" if outcome == "skipped" else "done"), now
        w.result = {"outcome": outcome, "signals": [x.key for x in signals_out]}
        channels = list(s.channels)
        user_id = w.user_id
        if outcome == "delivered":
            await notify(
                session,
                business_id,
                kind="assistant.brief" if brief else "assistant.nudge",
                level="warning" if any(x.priority == "high" for x in signals_out) else "info",
                title=title,
                body=body,
                link="/today",
                user_id=user_id,
            )
        await append(
            session,
            business_id=business_id,
            type="spine.assistant.woke",
            actor=Actor.agent("assistant"),
            subject=("agent_wakeup", wakeup_id),
            data={"wakeup_id": str(wakeup_id), "kind": w.kind, "outcome": outcome, "signals": len(signals_out)},
        )
        await plan_wakeups(session, business, user_id, now)
    if outcome == "delivered":
        text = await polish(services, business_id, body)
        for channel in channels:
            deliverer = _DELIVERERS.get(channel)
            if deliverer is not None:
                await deliverer(services, business_id, user_id, title, text)
    return {"outcome": outcome, "signals": [x.key for x in signals_out]}


async def brief_now(services: Services, business_id: uuid.UUID, user_id: uuid.UUID) -> dict[str, Any]:
    async with services.db.tenant(business_id) as session:
        w = AgentWakeup(
            business_id=business_id,
            user_id=user_id,
            wake_at=utcnow(),
            kind="brief",
            reason="Asked for it",
            dedupe_key=f"brief-now:{user_id}:{uuid.uuid4().hex}",
        )
        session.add(w)
        await session.flush()
        wakeup_id = w.id
    return await run_wakeup(services, business_id, wakeup_id)


def wakeup_view(w: AgentWakeup) -> dict[str, Any]:
    return {
        "id": str(w.id),
        "wake_at": w.wake_at.isoformat(),
        "kind": w.kind,
        "reason": w.reason,
        "status": w.status,
        "result": w.result,
    }
