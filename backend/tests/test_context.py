"""Our own memory (history, forgetting, no invented or secret notes) and the assistant staying on."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from projectx.kernel.time import utcnow
from projectx.services import Services
from projectx.spine.calendar.models import CalendarBusy, CalendarConnection, CalendarEvent, Reminder
from projectx.spine.context import proactive
from projectx.spine.context.models import AgentWakeup, ContextEntry
from projectx.spine.context.store import Learning, learn_from
from projectx.spine.gateway.contracts import ModelRequest, ModelResult
from projectx.spine.notifications.models import Notification

ALWAYS_AWAKE = {"quiet_start": "03:00", "quiet_end": "03:01"}  # tests must not depend on the clock


async def test_entries_keep_history_and_forgetting_wipes_every_version(founder: Any, business: dict[str, Any]) -> None:
    base = f"/api/v1/b/{business['id']}/context"
    first = await founder.post(base, json={"kind": "preference", "key": "meetings.start", "value": "Meetings after 10"})
    assert first.status_code == 201, first.text
    again = await founder.post(
        base, json={"kind": "preference", "key": "Meetings.Start", "value": "meetings after 10."}
    )
    assert again.json()["id"] == first.json()["id"]  # same belief: confirmed, not duplicated
    changed = await founder.post(
        base, json={"kind": "preference", "key": "meetings.start", "value": "Meetings after 11"}
    )
    history = (await founder.get(f"{base}/{changed.json()['id']}/history")).json()["items"]
    assert [(h["value"], h["status"]) for h in history] == [
        ("Meetings after 11", "active"),
        ("Meetings after 10", "superseded"),
    ]
    listed = (await founder.get(base)).json()["items"]
    assert [e["value"] for e in listed] == ["Meetings after 11"]

    secret = await founder.post(base, json={"kind": "fact", "key": "bank.card", "value": "Card 4111 1111 1111 1111"})
    assert secret.status_code == 422  # never stored

    assert (await founder.delete(f"{base}/{changed.json()['id']}")).status_code == 204
    assert (await founder.get(base)).json()["items"] == []
    gone = (await founder.get(f"{base}/{changed.json()['id']}/history")).json()["items"]
    assert {h["value"] for h in gone} == {"[forgotten]"}


async def test_learning_keeps_only_what_their_words_support(
    services: Services, founder: Any, business: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    bid, uid = uuid.UUID(business["id"]), uuid.UUID(founder.user["id"])
    due = (utcnow() + timedelta(days=5)).date().isoformat()
    text = f"I prefer calls in the afternoon. I'll send Priya the media kit by {due}."
    saved = await learn_from(services, bid, uid, text)  # offline model
    kinds = sorted(e.kind for e in saved)
    assert kinds == ["commitment", "preference"]
    async with services.db.tenant(bid) as s:
        promise = (await s.execute(select(ContextEntry).where(ContextEntry.kind == "commitment"))).scalar_one()
        reminder = await s.get(Reminder, uuid.UUID(promise.data["reminder_id"]))
    assert reminder is not None and reminder.kind == "follow_up" and "media kit" in reminder.notes

    async def invents(business_id: uuid.UUID, req: ModelRequest, **_: Any) -> ModelResult:
        parsed = Learning.model_validate(
            {
                "items": [
                    {  # not in their words: must be dropped
                        "kind": "fact",
                        "key": "person.ravi",
                        "value": "Ravi is their accountant",
                        "evidence": "Ravi does my books",
                        "confidence": 0.95,
                        "for_whole_business": False,
                        "due": "",
                    },
                    {  # a secret, even though quoted: must be dropped
                        "kind": "fact",
                        "key": "wifi",
                        "value": "The office wifi password is: hunter22",
                        "evidence": "password is: hunter22",
                        "confidence": 0.95,
                        "for_whole_business": True,
                        "due": "",
                    },
                    {
                        "kind": "routine",
                        "key": "routine.gym",
                        "value": "Goes to the gym on Tuesday mornings",
                        "evidence": "gym on tuesday mornings",
                        "confidence": 0.9,
                        "for_whole_business": False,
                        "due": "",
                    },
                ]
            }
        )
        return ModelResult("", parsed, "scripted", "scripted", req.rung, 0, False, 1)

    monkeypatch.setattr(services.gateway, "run", invents)
    kept = await learn_from(services, bid, uid, "Keep Tuesday free, gym on Tuesday mornings. The password is: hunter22")
    assert [e.key for e in kept] == ["routine.gym"]


async def test_the_assistant_reads_its_own_notes(
    services: Services, founder: Any, business: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    base = f"/api/v1/b/{business['id']}"
    await founder.post(
        f"{base}/context",
        json={"kind": "preference", "key": "meetings.morning", "value": "Morning means 11:00", "pinned": True},
    )
    seen: list[str] = []

    async def model(business_id: uuid.UUID, req: ModelRequest, **_: Any) -> ModelResult:
        from projectx.spine.assistant.service import Plan

        seen.append(req.system)
        return ModelResult(
            "",
            Plan.model_validate(
                {
                    "understood": "?",
                    "reflection": "",
                    "clarify": "Which day?",
                    "corrects_previous": False,
                    "lesson": "",
                    "steps": [],
                }
            ),
            "scripted",
            "scripted",
            req.rung,
            0,
            False,
            1,
        )

    monkeypatch.setattr(services.gateway, "run", model)
    res = await founder.post(f"{base}/assistant/messages", json={"text": "book a morning call"})
    assert res.json()["status"] == "asked"
    assert "Morning means 11:00" in seen[0]
    async with services.db.tenant(uuid.UUID(business["id"])) as s:
        from projectx.spine.jobs.models import Job

        jobs = (await s.execute(select(Job.kind).where(Job.business_id == uuid.UUID(business["id"])))).scalars().all()
    assert "spine.context.learn" in jobs  # every message is also learned from, in the background


async def test_brief_front_loads_what_matters_and_never_repeats(
    services: Services, founder: Any, business: dict[str, Any]
) -> None:
    base = f"/api/v1/b/{business['id']}"
    await founder.put(f"{base}/assistant/settings", json=ALWAYS_AWAKE)
    soon = utcnow() + timedelta(hours=3)
    made = await founder.post(
        f"{base}/calendar/events",
        json={
            "title": "Advertiser call",
            "starts_at": soon.isoformat(),
            "attendees": [{"email": "buyer@brand.example", "name": "Asha"}],
            "allow_overlap": True,
        },
    )
    assert made.status_code == 201, made.text
    before = len(services.sent_emails)  # type: ignore[attr-defined]
    out = (await founder.post(f"{base}/assistant/brief")).json()
    assert out["outcome"] == "delivered"
    assert out["signals"][0].startswith("invite:")  # the unapproved invitation leads
    bid = uuid.UUID(business["id"])
    async with services.db.tenant(bid) as s:
        note = (await s.execute(select(Notification).where(Notification.kind == "assistant.brief"))).scalars().one()
    assert note.title.startswith("Good morning, Ada: “Advertiser call” is at")
    assert note.level == "warning"
    mails = [m for m in services.sent_emails[before:] if m["subject"] == note.title]  # type: ignore[attr-defined]
    assert len(mails) == 1 and "Advertiser call" in mails[0]["text"]

    # A check right after has nothing new to say: it stays quiet and calls no model.
    async with services.db.tenant(bid) as s:
        await proactive.wake_soon(s, bid, uuid.UUID(founder.user["id"]), reason="test", key="t1")
        wakeup_id = (await s.execute(select(AgentWakeup.id).where(AgentWakeup.dedupe_key == "t1"))).scalar_one()
    again = await proactive.run_wakeup(services, bid, wakeup_id)
    assert again["outcome"] == "quiet"

    planned = (await founder.get(f"{base}/assistant/wakeups")).json()["items"]
    reasons = {w["reason"] for w in planned if w["status"] == "pending"}
    assert "Morning brief" in reasons and "Before “Advertiser call”" in reasons


async def test_a_calendar_clash_wakes_it_up(services: Services, founder: Any, business: dict[str, Any]) -> None:
    base = f"/api/v1/b/{business['id']}"
    bid, uid = uuid.UUID(business["id"]), uuid.UUID(founder.user["id"])
    await founder.put(f"{base}/assistant/settings", json=ALWAYS_AWAKE)
    start = utcnow() + timedelta(hours=5)
    await founder.post(f"{base}/calendar/events", json={"title": "Design review", "starts_at": start.isoformat()})
    async with services.db.tenant(bid) as s:
        secret_id = await services.vault.put(s, bid, kind="oauth_token", label="t", secret={"access_token": "x"})
        c = CalendarConnection(business_id=bid, provider="google", label="me", secret_id=secret_id, connected_by=uid)
        s.add(c)
        await s.flush()
        s.add(CalendarBusy(business_id=bid, connection_id=c.id, starts_at=start, ends_at=start + timedelta(hours=1)))
        await proactive.wake_soon(s, bid, uid, reason="Calendar changed", key="sync:test")
        wakeup_id = (await s.execute(select(AgentWakeup.id).where(AgentWakeup.dedupe_key == "sync:test"))).scalar_one()
    out = await proactive.run_wakeup(services, bid, wakeup_id)
    assert out["outcome"] == "delivered" and out["signals"][0].startswith("clash:")
    async with services.db.tenant(bid) as s:
        note = (await s.execute(select(Notification).where(Notification.kind == "assistant.nudge"))).scalars().one()
        event = (await s.execute(select(CalendarEvent))).scalars().one()
    assert note.title.startswith("Clash at") and "Design review" in note.title and event.title == "Design review"


async def test_quiet_hours_and_switching_off_are_respected(
    services: Services, founder: Any, business: dict[str, Any]
) -> None:
    base = f"/api/v1/b/{business['id']}"
    bid, uid = uuid.UUID(business["id"]), uuid.UUID(founder.user["id"])
    await founder.put(f"{base}/assistant/settings", json={"quiet_start": "00:00", "quiet_end": "23:59"})
    async with services.db.tenant(bid) as s:
        await proactive.wake_soon(s, bid, uid, reason="test", key="q1")
        q1 = (await s.execute(select(AgentWakeup.id).where(AgentWakeup.dedupe_key == "q1"))).scalar_one()
    assert (await proactive.run_wakeup(services, bid, q1))["outcome"] == "skipped"
    await founder.put(f"{base}/assistant/settings", json={"enabled": False, **ALWAYS_AWAKE})
    assert (await founder.post(f"{base}/assistant/brief")).json()["outcome"] == "skipped"


def test_polish_may_not_change_facts() -> None:
    facts = "“Board call” is at 15:00. 2 decisions waiting for you."
    assert proactive._facts_kept(facts, "Board call at 15:00, and 2 decisions need you.")
    assert not proactive._facts_kept(facts, "Board call at 16:00, and 2 decisions need you.")
    assert not proactive._facts_kept(facts, "Board call at 15:00, and 3 decisions need you.")


def test_compose_puts_the_most_important_first() -> None:
    signals = [
        proactive.Signal("day", "info", "Today, 2 meetings."),
        proactive.Signal("late", "medium", "Overdue: renew the domain."),
        proactive.Signal("clash", "high", "Clash at 15:00."),
    ]
    title, body = proactive.compose(signals, brief=True, first_name="Ada")
    assert title == "Good morning, Ada: Clash at 15:00."
    assert body.split("\n") == ["Clash at 15:00.", "Overdue: renew the domain.", "Today, 2 meetings."]
    assert isinstance(datetime.now(), datetime)
