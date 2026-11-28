"""Calendar and reminders: free time, scheduling, invitations through approval, reminders the model captures."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from projectx.kernel.time import utcnow
from projectx.services import Services
from projectx.spine.calendar.ics import build_invite
from projectx.spine.calendar.models import CalendarEvent, Reminder
from projectx.spine.calendar.service import send_due_reminders
from projectx.spine.calendar.timing import (
    Interval,
    advance,
    free_slots,
    next_notify_at,
    validate_working_hours,
)
from projectx.spine.jobs.worker import Worker
from projectx.spine.notifications.models import Notification

HOURS = {d: [["09:00", "17:00"]] for d in ("mon", "tue", "wed", "thu", "fri")}
IST = ZoneInfo("Asia/Kolkata")


def _utc(y: int, m: int, d: int, hh: int, mm: int = 0, tz: ZoneInfo = IST) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=tz).astimezone(UTC)


# ---------------------------------------------------------------- pure timing


def test_free_slots_respect_hours_busy_time_buffer_and_notice() -> None:
    monday = _utc(2026, 12, 7, 0)
    busy = [Interval(_utc(2026, 12, 7, 10), _utc(2026, 12, 7, 11))]
    slots = free_slots(
        hours=HOURS,
        busy=busy,
        start=monday,
        end=monday + timedelta(days=1),
        duration=timedelta(minutes=30),
        tz_name="Asia/Kolkata",
        now=_utc(2026, 12, 7, 8),
        buffer=timedelta(minutes=10),
        min_notice=timedelta(hours=1),
        step=timedelta(minutes=30),
        limit=100,
    )
    local = [s.start.astimezone(IST).strftime("%H:%M") for s in slots]
    assert local[0] == "09:00"  # 08:00 + one hour notice
    assert "09:30" not in local  # would end at 10:00, inside the 10-minute buffer
    assert "10:00" not in local and "10:30" not in local and "11:00" not in local
    assert "11:30" in local  # 11:10 is clear, and slots start on the half hour
    assert local[-1] == "16:30"  # last slot ends at 17:00
    assert all(s.end - s.start == timedelta(minutes=30) for s in slots)


def test_free_slots_skip_weekends_limit_per_day_and_follow_dst() -> None:
    ny = ZoneInfo("America/New_York")
    # Clocks went forward on Sunday 8 March 2026; Monday 9:00 is then 13:00 UTC, not 14:00.
    start = datetime(2026, 3, 6, 0, tzinfo=ny).astimezone(UTC)  # Friday
    slots = free_slots(
        hours=HOURS,
        busy=[],
        start=start,
        end=start + timedelta(days=4),
        duration=timedelta(hours=1),
        tz_name="America/New_York",
        now=start - timedelta(days=1),
        per_day=2,
        limit=10,
    )
    days = [s.start.astimezone(ny).date() for s in slots]
    assert days == [date(2026, 3, 6)] * 2 + [date(2026, 3, 9)] * 2
    assert slots[0].start.hour == 14 and slots[2].start.hour == 13
    assert all(s.start.astimezone(ny).time() >= time(9) for s in slots)


def test_working_hours_are_validated() -> None:
    assert validate_working_hours({"mon": [["13:00", "17:00"], ["09:00", "12:00"]]})["mon"][0] == ["09:00", "12:00"]
    for bad in ({"mon": [["17:00", "09:00"]]}, {"mon": [["09:00", "13:00"], ["12:00", "14:00"]]}, {"funday": []}):
        try:
            validate_working_hours(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")


def test_reminder_timing() -> None:
    now = datetime(2026, 12, 1, 12, tzinfo=UTC)
    due = now + timedelta(days=10)
    leads = [30 * 1440, 7 * 1440, 1440]
    # The 30-day warning is already past: fire once now, then 7 days before, then 1 day before, then on time.
    assert next_notify_at(due, leads, last_notified_at=None, now=now) == now
    assert next_notify_at(due, leads, last_notified_at=now, now=now) == due - timedelta(days=7)
    assert next_notify_at(due, leads, last_notified_at=due - timedelta(days=1), now=now) == due
    assert next_notify_at(due, leads, last_notified_at=due, now=now) is None
    assert next_notify_at(now + timedelta(days=60), [1440], last_notified_at=None, now=now) == now + timedelta(days=59)
    assert next_notify_at(now - timedelta(hours=1), [0], last_notified_at=None, now=now) is None


def test_repeats_keep_the_local_time_and_clamp_month_ends() -> None:
    jan31 = _utc(2026, 1, 31, 9)
    assert advance(jan31, "monthly", "Asia/Kolkata").astimezone(IST) == datetime(2026, 2, 28, 9, tzinfo=IST)
    assert advance(jan31, "yearly", "Asia/Kolkata").astimezone(IST).date() == date(2027, 1, 31)
    ny = ZoneInfo("America/New_York")
    before_dst = datetime(2026, 3, 7, 9, tzinfo=ny).astimezone(UTC)
    assert advance(before_dst, "daily", "America/New_York").astimezone(ny).hour == 9


def test_ics_escapes_and_folds() -> None:
    now = datetime(2026, 12, 1, tzinfo=UTC)
    ics = build_invite(
        uid="abc@projectx",
        method="REQUEST",
        sequence=2,
        title="Plan, review; and ship",
        description="Line one\nLine two " + "x" * 120,
        location=None,
        starts_at=now,
        ends_at=now + timedelta(hours=1),
        organizer_email="no-reply@example.com",
        organizer_name='Biz "Co"\r\nINJECT',
        attendees=[{"email": "ravi@example.com", "name": "Ravi"}],
        now=now,
    ).decode()
    assert "SUMMARY:Plan\\, review\\; and ship" in ics
    assert "SEQUENCE:2" in ics and "METHOD:REQUEST" in ics
    assert "\r\nINJECT" not in ics
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))
    unfolded = ics.replace("\r\n ", "")
    assert "ATTENDEE;ROLE=REQ-PARTICIPANT" in unfolded and 'CN="Ravi":mailto:ravi@example.com' in unfolded
    assert "x" * 120 in unfolded


# ---------------------------------------------------------------- API


def _next_monday(hour: int) -> datetime:
    today = utcnow().astimezone(IST).date()
    monday = today + timedelta(days=(7 - today.weekday()) or 7) + timedelta(days=7)
    return datetime.combine(monday, time(hour), tzinfo=IST)


async def test_schedule_find_free_time_and_block_double_booking(founder: Any, business: dict[str, Any]) -> None:
    base = f"/api/v1/b/{business['id']}"
    put = await founder.put(f"{base}/calendar/settings", json={"working_hours": HOURS, "buffer_minutes": 0})
    assert put.status_code == 200, put.text
    start = _next_monday(10)
    res = await founder.post(
        f"{base}/calendar/events", json={"title": "Focus: plan Q1", "starts_at": start.isoformat(), "kind": "focus"}
    )
    assert res.status_code == 201, res.text
    event = res.json()
    assert event["invite_decision_id"] is None  # nobody to invite, so nothing to approve
    assert datetime.fromisoformat(event["ends_at"]) - start == timedelta(minutes=30)

    clash = await founder.post(
        f"{base}/calendar/events", json={"title": "Clash", "starts_at": (start + timedelta(minutes=15)).isoformat()}
    )
    assert clash.status_code == 409 and clash.json()["code"] == "slot_taken"

    day = start.replace(hour=0)
    free = await founder.get(
        f"{base}/calendar/free",
        params={"start": day.isoformat(), "end": (day + timedelta(days=1)).isoformat(), "duration_minutes": 60},
    )
    assert free.status_code == 200, free.text
    starts = [datetime.fromisoformat(s["starts_at"]).astimezone(IST).strftime("%H:%M") for s in free.json()["slots"]]
    assert starts[:2] == ["09:00", "10:30"]  # 09:30 and 10:00 would overlap the focus block
    assert "16:00" in starts and "16:30" not in starts

    reminders = (await founder.get(f"{base}/reminders", params={"kind": "meeting"})).json()["items"]
    assert any(r["event_id"] == event["id"] and r["notify_before_minutes"] == [1440, 15] for r in reminders)


async def test_invitations_wait_for_approval_and_follow_changes(
    services: Services, founder: Any, business: dict[str, Any]
) -> None:
    base = f"/api/v1/b/{business['id']}"
    start = _next_monday(15)
    res = await founder.post(
        f"{base}/calendar/events",
        json={
            "title": "Media kit walkthrough",
            "starts_at": start.isoformat(),
            "duration_minutes": 45,
            "attendees": [{"email": "Priya@Example.com", "name": "Priya"}, {"email": "priya@example.com"}],
            "allow_overlap": True,
        },
    )
    assert res.status_code == 201, res.text
    event = res.json()
    assert event["attendees"] == [{"email": "priya@example.com", "name": "Priya"}]
    first = event["invite_decision_id"]
    decision = (await founder.get(f"{base}/decisions/{first}")).json()
    assert decision["status"] == "pending" and decision["action_type"] == "spine.calendar.send_invite"

    # Moving it before anyone approved retires the old card: only the latest time goes out.
    moved = await founder.post(
        f"{base}/calendar/events/{event['id']}/reschedule",
        json={"starts_at": (start + timedelta(hours=1)).isoformat(), "allow_overlap": True},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["sequence"] == 1
    assert (await founder.get(f"{base}/decisions/{first}")).json()["status"] == "cancelled"
    second = moved.json()["invite_decision_id"]

    before = len(services.sent_emails)  # type: ignore[attr-defined]
    assert (await founder.post(f"{base}/decisions/{second}/approve", json={})).status_code == 200
    await Worker(services).run_until_idle()
    mails = [m for m in services.sent_emails[before:] if m.get("attachments")]  # type: ignore[attr-defined]
    assert [m["to"] for m in mails] == ["priya@example.com"]
    ics = mails[0]["attachments"][0]
    assert ics.mime == "text/calendar" and ics.params == {"method": "REQUEST"}
    assert b"SEQUENCE:1" in ics.data and b"Media kit walkthrough" in ics.data
    view = (await founder.get(f"{base}/calendar/events/{event['id']}")).json()
    assert view["invited"] and view["invite_up_to_date"]

    cancelled = await founder.post(f"{base}/calendar/events/{event['id']}/cancel")
    assert cancelled.json()["status"] == "cancelled"
    cancel_card = (await founder.get(f"{base}/decisions/{cancelled.json()['invite_decision_id']}")).json()
    assert cancel_card["payload"]["method"] == "CANCEL"
    meeting_reminders = (await founder.get(f"{base}/reminders", params={"status": "dismissed"})).json()["items"]
    assert any(r["event_id"] == event["id"] for r in meeting_reminders)


async def test_model_captures_what_to_remember(services: Services, founder: Any, business: dict[str, Any]) -> None:
    base = f"/api/v1/b/{business['id']}"
    soon = (utcnow() + timedelta(days=40)).date().isoformat()
    text = (
        f"Your domain bought.news expires on {soon}, renew it every year to keep it. "
        "Thanks for shopping with us! "
        "Old note: the launch party was on 2020-01-05."
    )
    res = await founder.post(f"{base}/reminders/capture", json={"text": text})
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body["created"]) == 1, body
    r = body["created"][0]
    assert r["kind"] == "renewal" and r["repeat"] == "yearly" and r["source"] == "agent"
    assert r["notify_before_minutes"] == [43200, 10080, 1440]  # 30, 7 and 1 day before
    assert datetime.fromisoformat(r["due_at"]).astimezone(IST).hour == 9  # date only: 09:00 local
    assert any(s["reason"] == "Already in the past" for s in body["skipped"])

    again = (await founder.post(f"{base}/reminders/capture", json={"text": text})).json()
    assert again["created"] == [] and any(s["reason"] == "Already stored" for s in again["skipped"])


async def test_due_reminders_fire_and_repeat(services: Services, founder: Any, business: dict[str, Any]) -> None:
    base = f"/api/v1/b/{business['id']}"
    bid = uuid.UUID(business["id"])
    due = utcnow() + timedelta(days=3)
    made = await founder.post(
        f"{base}/reminders",
        json={
            "title": "Pay the hosting bill",
            "due_at": due.isoformat(),
            "kind": "payment",
            "importance": "high",
            "repeat": "monthly",
        },
    )
    assert made.status_code == 201, made.text
    rid = uuid.UUID(made.json()["id"])
    assert datetime.fromisoformat(made.json()["next_notify_at"]) <= utcnow()  # "3 days before" is already now

    await send_due_reminders(services, utcnow() + timedelta(seconds=1))
    async with services.db.tenant(bid) as s:
        r = await s.get(Reminder, rid)
        notes = (await s.execute(select(Notification).where(Notification.kind == "reminder"))).scalars().all()
    assert r is not None and r.last_notified_at is not None
    assert r.next_notify_at is not None and abs(r.next_notify_at - (due - timedelta(days=1))) < timedelta(seconds=1)
    assert any(n.title.startswith("Pay the hosting bill is in") and n.level == "warning" for n in notes)

    done = (await founder.post(f"{base}/reminders/{rid}/done")).json()
    assert done["status"] == "active"  # monthly: it moves on instead of closing
    assert datetime.fromisoformat(done["due_at"]) > due + timedelta(days=27)


async def test_erasure_removes_attendee_emails(services: Services, business: dict[str, Any]) -> None:
    from projectx.modules.gtm.compliance.service import erase_contact, export_contact
    from projectx.spine.ledger.service import Actor
    from projectx.spine.twin.models import Contact

    bid = uuid.UUID(business["id"])
    async with services.db.tenant(bid) as s:
        contact = Contact(business_id=bid, email="ana@example.com", name="Ana", stage="lead")
        s.add(contact)
        await s.flush()
        event = CalendarEvent(
            business_id=bid,
            title="Call with Ana",
            starts_at=utcnow() + timedelta(days=2),
            ends_at=utcnow() + timedelta(days=2, hours=1),
            attendees=[{"email": "ana@example.com", "name": "Ana"}, {"email": "sam@example.com", "name": "Sam"}],
        )
        s.add(event)
        await s.flush()
        exported = await export_contact(s, contact)
        assert [e["title"] for e in exported["calendar_events"]] == ["Call with Ana"]
        await erase_contact(s, contact, actor=Actor.system("test"))
        await s.flush()
        await s.refresh(event)
        assert [a["email"] for a in event.attendees] == ["erased@invalid", "sam@example.com"]
