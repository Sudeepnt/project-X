"""Strategy reads every department; handshake lets two businesses' assistants agree a meeting, sharing only
free/busy time, and anything beyond what was granted is refused and reported."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

import httpx
from sqlalchemy import select

from projectx.modules.strategy import service as strategy
from projectx.modules.strategy.models import Review
from projectx.services import Services
from projectx.spine.calendar.models import CalendarEvent
from projectx.spine.jobs.worker import Worker
from projectx.spine.notifications.models import Notification
from tests.conftest import new_business, sign_up

HOURS = {d: [["09:00", "17:00"]] for d in ("mon", "tue", "wed", "thu", "fri")}


def url(business: dict[str, Any], path: str) -> str:
    return f"/api/v1/b/{business['id']}{path}"


def test_cross_department_risk_rules() -> None:
    n = {
        "finance": {
            "runway_months": 2.5,
            "cash_minor": 100_000,
            "revenue_mtd_minor": 10_000,
            "receivables_overdue_minor": 50_000,
            "currency": "INR",
        },
        "people": {"monthly_payroll_minor": 400_000},
        "ops": {"tickets_past_sla": 2, "overdue_tasks": 0},
        "legal": {"unsigned_contracts": 1, "open_access_tasks": 0},
    }
    keys = {(r.key, r.severity) for r in strategy.find_risks(n)}
    assert keys == {
        ("runway", "high"),
        ("payroll_cover", "high"),
        ("receivables", "medium"),
        ("sla", "medium"),
        ("unsigned", "low"),
    }
    assert strategy.find_risks({}) == []


async def test_overview_objectives_and_weekly_review(
    services: Services, founder: Any, business: dict[str, Any]
) -> None:
    bid = uuid.UUID(business["id"])
    over = (await founder.get(url(business, "/strategy/overview"))).json()
    assert {"finance", "ops", "people", "legal", "product"} <= set(over["numbers"])
    bad = await founder.post(
        url(business, "/strategy/objectives"),
        json={
            "title": "x",
            "metric": "nowhere.thing",
            "target": 1,
            "due_date": (date.today() + timedelta(days=30)).isoformat(),
        },
    )
    assert bad.status_code == 422
    ok = await founder.post(
        url(business, "/strategy/objectives"),
        json={
            "title": "Answer every ticket on time",
            "metric": "ops.tickets_past_sla",
            "target": 0,
            "direction": "down",
            "due_date": (date.today() + timedelta(days=30)).isoformat(),
        },
    )
    assert ok.status_code == 201, ok.text
    await strategy.weekly(services, bid)
    await strategy.weekly(services, bid)  # once a week
    async with services.db.tenant(bid) as s:
        reviews = (await s.execute(select(Review))).scalars().all()
    assert len(reviews) == 1 and reviews[0].objectives[0]["status"] == "met"


async def test_two_businesses_agree_a_meeting_and_nothing_else_leaks(
    services: Services, app: Any, founder: Any, business: dict[str, Any]
) -> None:
    # The other business signs in on its own client: sharing one would swap the session cookie.
    other_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    other_founder = await sign_up(other_client)
    other = await new_business(other_founder, name="Print Co")
    for who, biz in ((founder, business), (other_founder, other)):
        await who.put(
            url(biz, "/calendar/settings"), json={"working_hours": HOURS, "buffer_minutes": 0, "min_notice_minutes": 0}
        )

    # Both are busy at different times early on; the first common free slot is where neither is booked.
    monday = date.today() + timedelta(days=(7 - date.today().weekday()) % 7 + 7)
    await founder.post(
        url(business, "/calendar/events"),
        json={"title": "Busy A", "starts_at": f"{monday}T09:00:00", "duration_minutes": 60},
    )
    await other_founder.post(
        url(other, "/calendar/events"),
        json={"title": "Secret board call", "starts_at": f"{monday}T10:00:00", "duration_minutes": 60},
    )

    code = (await founder.post(url(business, "/network/handshake/invites"), json={"scopes": ["availability"]})).json()[
        "code"
    ]
    # Print Co links back but shares nothing yet.
    link = (await other_founder.post(url(other, "/network/handshake/accept"), json={"code": code, "grant": []})).json()
    assert link.get("status") == "active", link
    again = await other_founder.post(url(other, "/network/handshake/accept"), json={"code": code, "grant": []})
    assert again.status_code == 422  # codes work once

    my_link = (await founder.get(url(business, "/network/handshake/links"))).json()["items"][0]
    refused = await founder.post(
        url(business, f"/network/handshake/links/{my_link['id']}/meet"), json={"title": "Print run", "within_days": 14}
    )
    assert refused.status_code == 403
    async with services.db.tenant(uuid.UUID(other["id"])) as s:
        warned = (await s.execute(select(Notification).where(Notification.kind == "network.denied"))).scalars().all()
    assert len(warned) == 1  # they were told someone asked for what they hadn't shared

    # Print Co now shares availability; the assistants find a time without anyone trading messages.
    code2 = (
        await other_founder.post(url(other, "/network/handshake/invites"), json={"scopes": ["availability"]})
    ).json()["code"]
    await founder.post(url(business, "/network/handshake/accept"), json={"code": code2, "grant": ["availability"]})
    proposal = (
        await founder.post(
            url(business, f"/network/handshake/links/{my_link['id']}/meet"),
            json={"title": "Print run", "within_days": 14},
        )
    ).json()
    assert proposal["status"] == "proposed", proposal
    theirs = (await other_founder.get(url(other, "/network/handshake/meetings"))).json()["items"]
    assert [m["role"] for m in theirs] == ["invited"]
    cards = (await other_founder.get(url(other, "/decisions"), params={"status": "pending"})).json()["items"]
    card = next(c for c in cards if c["action_type"] == "network.accept_meeting")
    assert "Secret board call" not in str(card)  # they only ever learn free/busy
    assert (await other_founder.post(url(other, f"/decisions/{card['id']}/approve"), json={})).status_code == 200
    await Worker(services).run_until_idle()

    for biz in (business, other):
        async with services.db.tenant(uuid.UUID(biz["id"])) as s:
            booked = (
                (await s.execute(select(CalendarEvent).where(CalendarEvent.title.like("Print run%")))).scalars().all()
            )
        assert len(booked) == 1
        assert booked[0].starts_at.isoformat() == proposal["starts_at"]
    mine = (await founder.get(url(business, "/network/handshake/meetings"))).json()["items"]
    assert [m["status"] for m in mine] == ["booked"]
    await other_client.aclose()
