"""The assistant: code checks each plan, failed checks lead to reflection and a retry, corrections are remembered."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from projectx.kernel.time import utcnow
from projectx.services import Services
from projectx.spine.assistant.service import Plan
from projectx.spine.calendar.models import CalendarEvent
from projectx.spine.gateway.contracts import ModelRequest, ModelResult
from projectx.spine.memory.models import MemoryCase
from projectx.spine.twin.models import Contact

IST = ZoneInfo("Asia/Kolkata")
HOURS = {d: [["09:00", "17:00"]] for d in ("mon", "tue", "wed", "thu", "fri")}


def _monday(hour: int) -> datetime:
    today = utcnow().astimezone(IST).date()
    monday = today + timedelta(days=(7 - today.weekday()) or 7) + timedelta(days=7)
    return datetime.combine(monday, time(hour), tzinfo=IST)


def local(moment: datetime) -> str:
    return moment.astimezone(IST).strftime("%Y-%m-%dT%H:%M")


def step(op: str, **kw: Any) -> dict[str, Any]:
    base = {
        "op": op,
        "title": "",
        "when": "",
        "duration_minutes": 0,
        "people": [],
        "event_ref": "",
        "search_from": "",
        "search_to": "",
        "reminder_kind": "other",
        "repeat": "none",
        "notes": "",
    }
    return base | kw


def plan(*steps: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return {
        "understood": kw.pop("understood", "They want something done"),
        "reflection": kw.pop("reflection", ""),
        "clarify": kw.pop("clarify", ""),
        "corrects_previous": kw.pop("corrects_previous", False),
        "lesson": kw.pop("lesson", ""),
        "steps": list(steps),
    }


class ScriptedModel:
    """Stands in for the gateway: answers from a script and records every request."""

    def __init__(self) -> None:
        self.script: list[dict[str, Any] | Callable[[ModelRequest], dict[str, Any]]] = []
        self.requests: list[ModelRequest] = []

    async def run(self, business_id: uuid.UUID, req: ModelRequest, **_: Any) -> ModelResult:
        self.requests.append(req)
        nxt = self.script.pop(0)
        body = nxt(req) if callable(nxt) else nxt
        parsed = Plan.model_validate(body)
        return ModelResult("", parsed, "scripted", "scripted", req.rung, 0, False, 1)


@pytest.fixture
def model(services: Services, monkeypatch: pytest.MonkeyPatch) -> ScriptedModel:
    fake = ScriptedModel()
    monkeypatch.setattr(services.gateway, "run", fake.run)
    return fake


async def _say(founder: Any, business: dict[str, Any], text: str) -> dict[str, Any]:
    res = await founder.post(f"/api/v1/b/{business['id']}/assistant/messages", json={"text": text})
    assert res.status_code == 200, res.text
    body: dict[str, Any] = res.json()
    return body


async def _setup(founder: Any, business: dict[str, Any]) -> None:
    await founder.put(
        f"/api/v1/b/{business['id']}/calendar/settings", json={"working_hours": HOURS, "buffer_minutes": 0}
    )


async def test_failed_checks_lead_to_reflection_and_a_corrected_plan(
    services: Services, founder: Any, business: dict[str, Any], model: ScriptedModel
) -> None:
    await _setup(founder, business)
    past = utcnow() - timedelta(days=3)
    model.script = [
        plan(step("schedule", title="Pitch review", when=local(past)), understood="Pitch review on Monday"),
        plan(
            step("schedule", title="Pitch review", when=local(_monday(11))),
            understood="Pitch review on Monday",
            reflection="I took 'Monday' as last Monday; relative days count forward from today.",
        ),
    ]
    out = await _say(founder, business, "set up a pitch review monday at 11")
    assert out["status"] == "done" and out["trials"] == 2
    assert out["reply"].startswith("Booked “Pitch review”")  # the outcome comes first
    assert out["reflections"] == ["I took 'Monday' as last Monday; relative days count forward from today."]
    retry = model.requests[1]
    assert "is in the past" in retry.messages[-1].content and "reflection" in retry.messages[-1].content
    assert model.requests[0].rung == "small" and retry.rung == "mid"  # cheapest first, climb on failure

    bid = uuid.UUID(business["id"])
    async with services.db.tenant(bid) as s:
        events = (await s.execute(select(CalendarEvent))).scalars().all()
        lessons = (await s.execute(select(MemoryCase).where(MemoryCase.kind == "assistant_lesson"))).scalars().all()
    assert [e.title for e in events] == ["Pitch review"] and events[0].source == "agent"
    assert [(c.title, c.weight) for c in lessons] == [("Learned while working", 0.5)]


async def test_busy_slot_offers_alternatives_and_it_asks_instead_of_guessing(
    founder: Any, business: dict[str, Any], model: ScriptedModel
) -> None:
    await _setup(founder, business)
    base = f"/api/v1/b/{business['id']}"
    await founder.post(f"{base}/calendar/events", json={"title": "Board call", "starts_at": _monday(15).isoformat()})

    def second(req: ModelRequest) -> dict[str, Any]:
        feedback = req.messages[-1].content
        assert "already busy" in feedback and "Nearest free" in feedback
        return plan(clarify="3pm Monday is taken by the board call. 15:30 or Tuesday 10:00?")

    model.script = [plan(step("schedule", title="Coffee with Sam", when=local(_monday(15)))), second]
    out = await _say(founder, business, "coffee with sam monday 3pm")
    assert out["status"] == "asked" and out["reply"].startswith("3pm Monday is taken")
    events = (await founder.get(f"{base}/calendar/events", params={"start": _monday(0).isoformat()})).json()["items"]
    assert [e["title"] for e in events] == ["Board call"]  # nothing booked on a guess


async def test_people_resolve_from_contacts_and_invites_wait_for_approval(
    services: Services, founder: Any, business: dict[str, Any], model: ScriptedModel
) -> None:
    await _setup(founder, business)
    bid = uuid.UUID(business["id"])
    async with services.db.tenant(bid) as s:
        s.add(Contact(business_id=bid, name="Priya Shah", email="priya@agency.example", stage="lead"))
        s.add(Contact(business_id=bid, name="Ravi Kumar", email="ravi@one.example", stage="lead"))
        s.add(Contact(business_id=bid, name="Ravi Menon", email="ravi@two.example", stage="lead"))

    def sees_people(req: ModelRequest) -> dict[str, Any]:
        assert "Priya Shah · priya@agency.example" in req.messages[0].content  # KNOWN PEOPLE in the prompt
        return plan(
            step(
                "schedule",
                title="Media kit walkthrough",
                when=local(_monday(10)),
                people=[{"name": "Priya", "email": ""}],
            )
        )

    model.script = [sees_people]
    out = await _say(founder, business, "Book Priya for the media kit walkthrough Monday 10am")
    assert out["status"] == "done", out
    assert "waiting for your OK" in out["reply"]
    decision = (await founder.get(f"/api/v1/b/{bid}/decisions/{out['results'][0]['decision_id']}")).json()
    assert decision["status"] == "pending"
    assert decision["preview"]["event"]["attendees"] == [{"email": "priya@agency.example", "name": "Priya"}]

    ask = plan(clarify="Ravi Kumar or Ravi Menon?")
    model.script = [
        plan(step("schedule", title="Sync", when=local(_monday(12)), people=[{"name": "Ravi", "email": ""}]))
    ]
    model.script += [ask]
    out = await _say(founder, business, "Sync with Ravi Monday noon")
    assert out["status"] == "asked" and out["reply"] == "Ravi Kumar or Ravi Menon?"
    assert "matches several people" in model.requests[-1].messages[-1].content


async def test_corrections_become_lessons_in_future_prompts(
    founder: Any, business: dict[str, Any], model: ScriptedModel
) -> None:
    await _setup(founder, business)
    model.script = [plan(step("schedule", title="Standup", when=local(_monday(9))))]
    first = await _say(founder, business, "standup monday morning")
    event_id = first["results"][0]["event_id"]

    def correction(req: ModelRequest) -> dict[str, Any]:
        assert "They said: standup monday morning" in req.messages[0].content  # short-term memory
        ref = uuid.UUID(event_id).hex[-6:]
        assert ref in req.messages[0].content
        return plan(
            step("reschedule", event_ref=ref, when=local(_monday(11))),
            corrects_previous=True,
            lesson="By 'morning' they mean 11:00, not 9:00.",
        )

    model.script = [correction]
    fixed = await _say(founder, business, "no, morning for me is 11")
    assert fixed["status"] == "done" and fixed["reply"].startswith("Moved “Standup”")

    def remembers(req: ModelRequest) -> dict[str, Any]:
        assert "By 'morning' they mean 11:00, not 9:00." in req.system
        return plan(step("add_reminder", title="Send the deck", when=local(_monday(11)), reminder_kind="task"))

    model.script = [remembers]
    out = await _say(founder, business, "remind me monday morning to send the deck")
    assert out["status"] == "done" and out["reply"].startswith("I'll remind you")


async def test_offline_model_handles_plain_requests_and_asks_otherwise(founder: Any, business: dict[str, Any]) -> None:
    await _setup(founder, business)
    when = _monday(16).strftime("%Y-%m-%d %H:%M")
    out = await _say(founder, business, f"Book a call with the printer {when}")
    assert out["status"] == "done" and "Booked" in out["reply"]
    unclear = await _say(founder, business, "sort out next week for me")
    assert unclear["status"] == "asked" and "2026-12-08 15:00" in unclear["reply"]
    turns = (await founder.get(f"/api/v1/b/{business['id']}/assistant/turns")).json()["items"]
    assert [t["status"] for t in turns] == ["asked", "done"]
