"""Legal and admin with the other departments: hire → contracts and account set-up; leave → access removed;
won deal → agreement to sign; renewal → Finance bill; uploaded contract → obligations quoted from it."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select

from projectx.modules.finance.models import Bill
from projectx.modules.legal import service as legal
from projectx.modules.legal.models import Access, Contract, Obligation
from projectx.services import Services
from projectx.spine.calendar.models import Reminder
from projectx.spine.decisions.models import Decision
from projectx.spine.jobs.worker import Worker


def url(business: dict[str, Any], path: str) -> str:
    return f"/api/v1/b/{business['id']}{path}"


async def test_hiring_and_leaving_open_and_close_every_door(
    services: Services, founder: Any, business: dict[str, Any]
) -> None:
    bid = uuid.UUID(business["id"])
    start = date.today() + timedelta(days=10)
    e = (
        await founder.post(
            url(business, "/people/employees"),
            json={
                "name": "Nikhil Das",
                "email": "nikhil@example.com",
                "role": "Designer",
                "start_date": start.isoformat(),
                "monthly_salary_minor": 150_000,
            },
        )
    ).json()
    await Worker(services).run_until_idle()
    contracts = (await founder.get(url(business, "/legal/contracts"))).json()["items"]
    assert sorted(c["kind"] for c in contracts) == ["employment", "nda"]
    employment = next(c for c in contracts if c["kind"] == "employment")
    assert "Role: Designer" in employment["body"] and start.isoformat() in employment["body"]
    async with services.db.tenant(bid) as s:
        grants = (await s.execute(select(Decision).where(Decision.action_type == "admin.grant_access"))).scalars().all()
    assert len(grants) == 4 and all(g.kind == "manual_task" for g in grants)
    for g in grants:
        assert (await founder.post(url(business, f"/decisions/{g.id}/approve"), json={})).status_code == 200
    await Worker(services).run_until_idle()
    doors = (await founder.get(url(business, "/admin/access"))).json()["items"]
    assert {d["status"] for d in doors} == {"granted"}

    await founder.post(
        url(business, f"/people/employees/{e['id']}/last-day"),
        json={"last_day": (start + timedelta(days=60)).isoformat()},
    )
    await Worker(services).run_until_idle()
    async with services.db.tenant(bid) as s:
        revokes = (
            (await s.execute(select(Decision).where(Decision.action_type == "admin.revoke_access"))).scalars().all()
        )
        statuses = {a.status for a in (await s.execute(select(Access))).scalars()}
    assert len(revokes) == 4 and statuses == {"to_revoke"}


async def test_won_deal_gets_an_agreement_signed_then_its_dates_remembered(
    services: Services, founder: Any, business: dict[str, Any]
) -> None:
    bid = uuid.UUID(business["id"])
    pipelines = (await founder.get(url(business, "/gtm/pipelines"))).json()["items"]
    advertisers = next(p for p in pipelines if p["key"] == "advertisers")
    contact = (
        await founder.post(
            url(business, "/gtm/contacts"), json={"email": "buyer@acme.example", "name": "Asha", "stage": "lead"}
        )
    ).json()
    deal = (
        await founder.post(
            url(business, "/gtm/deals"),
            json={
                "pipeline_id": advertisers["id"],
                "title": "Q1 sponsorship",
                "contact_id": contact["id"],
                "value_minor": 250_000,
            },
        )
    ).json()
    await founder.patch(url(business, f"/gtm/deals/{deal['id']}"), json={"stage_key": "advertiser"})
    await Worker(services).run_until_idle()
    agreement = next(
        c for c in (await founder.get(url(business, "/legal/contracts"))).json()["items"] if c["kind"] == "customer"
    )
    assert agreement["status"] == "draft" and "Q1 sponsorship" in agreement["body"]
    async with services.db.tenant(bid) as s:
        c = await s.get(Contract, uuid.UUID(agreement["id"]))
        assert c is not None
        c.end_date = date.today() + timedelta(days=200)
        c.notice_days = 30
    assert (
        await founder.post(url(business, f"/decisions/{agreement['decision_id']}/approve"), json={})
    ).status_code == 200
    await Worker(services).run_until_idle()
    async with services.db.tenant(bid) as s:
        c = await s.get(Contract, uuid.UUID(agreement["id"]))
        reminders = (
            (await s.execute(select(Reminder).where(Reminder.dedupe_key.like(f"contract:{agreement['id']}%"))))
            .scalars()
            .all()
        )
    assert c is not None and c.status == "active" and c.signed_at is not None
    assert sorted(r.kind for r in reminders) == ["deadline", "renewal"]


async def test_renewals_are_handed_to_finance_once(services: Services, founder: Any, business: dict[str, Any]) -> None:
    bid = uuid.UUID(business["id"])
    soon = date.today() + timedelta(days=5)
    await founder.post(
        url(business, "/admin/assets"),
        json={
            "kind": "domain",
            "name": "bought.news",
            "vendor": "Registrar Co",
            "cost_minor": 1_200,
            "billing": "yearly",
            "renews_on": soon.isoformat(),
        },
    )
    await legal.daily(services, bid)
    await legal.daily(services, bid)
    await Worker(services).run_until_idle()
    async with services.db.tenant(bid) as s:
        bills = (await s.execute(select(Bill))).scalars().all()
        pay = (await s.execute(select(Decision).where(Decision.action_type == "finance.pay_bill"))).scalars().all()
    assert [(b.supplier, b.amount_minor, b.due_date) for b in bills] == [("Registrar Co", 1_200, soon)]
    assert len(pay) == 1 and pay[0].status == "pending"


async def test_uploaded_contract_obligations_must_be_quoted(
    services: Services, founder: Any, business: dict[str, Any]
) -> None:
    bid = uuid.UUID(business["id"])
    due = (date.today() + timedelta(days=40)).isoformat()
    body = (
        f"This agreement is between the parties. The client shall pay the first instalment on {due}. "
        "The supplier delivers the report on 2001-01-01. Ordinary words follow here."
    )
    res = await founder.post(
        url(business, "/legal/contracts"),
        json={"kind": "supplier", "title": "Printing contract", "counterparty": "Print Co", "body": body},
    )
    assert res.status_code == 201, res.text
    await Worker(services).run_until_idle()
    async with services.db.tenant(bid) as s:
        obs = (await s.execute(select(Obligation))).scalars().all()
    assert [(o.kind, o.due_date.isoformat()) for o in obs] == [("payment", due)]  # the past one is dropped


def test_renewal_dates_roll_forward_safely() -> None:
    assert legal.next_renewal(date(2026, 1, 31), "monthly") == date(2026, 2, 28)
    assert legal.next_renewal(date(2028, 2, 29), "yearly") == date(2029, 2, 28)
    assert legal.next_renewal(date(2026, 12, 15), "monthly") == date(2027, 1, 15)
