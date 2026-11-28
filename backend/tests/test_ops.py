"""Operations working with the other departments: won deal → project and kickoff; milestones → invoices;
tickets answered only from help articles; feedback to Product; unpaid → on hold; erasure honoured."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update

from projectx.modules.finance.models import Invoice
from projectx.modules.ops.models import Project, Task, Ticket
from projectx.services import Services
from projectx.spine.calendar.models import CalendarEvent
from projectx.spine.decisions.models import Decision
from projectx.spine.jobs.worker import Worker
from projectx.spine.ledger.models import LedgerEvent
from projectx.spine.twin.models import Deal


def url(business: dict[str, Any], path: str) -> str:
    return f"/api/v1/b/{business['id']}{path}"


async def _deal(
    founder: Any, business: dict[str, Any], *, value: int = 90_000, win: bool = True
) -> tuple[dict[str, Any], dict[str, Any]]:
    pipelines = (await founder.get(url(business, "/gtm/pipelines"))).json()["items"]
    advertisers = next(p for p in pipelines if p["key"] == "advertisers")
    contact = (
        await founder.post(
            url(business, "/gtm/contacts"),
            json={"email": f"c-{uuid.uuid4().hex[:6]}@example.com", "name": "Mira Client", "stage": "lead"},
        )
    ).json()
    deal = (
        await founder.post(
            url(business, "/gtm/deals"),
            json={
                "pipeline_id": advertisers["id"],
                "title": "Launch campaign",
                "contact_id": contact["id"],
                "value_minor": value,
            },
        )
    ).json()
    if win:
        await founder.patch(url(business, f"/gtm/deals/{deal['id']}"), json={"stage_key": "advertiser"})
    return contact, deal


async def test_won_deal_starts_delivery_and_books_the_kickoff(
    services: Services, founder: Any, business: dict[str, Any]
) -> None:
    bid = uuid.UUID(business["id"])
    _, deal = await _deal(founder, business)
    await Worker(services).run_until_idle()
    projects = (await founder.get(url(business, "/ops/projects"))).json()["items"]
    assert len(projects) == 1
    p = projects[0]
    assert next(t["title"] for t in p["tasks"]) == "Kickoff call" and p["deal_id"] == deal["id"]
    async with services.db.tenant(bid) as s:
        kickoff = await s.get(CalendarEvent, uuid.UUID(p["kickoff_event_id"]))
        invite = (
            await s.execute(select(Decision).where(Decision.action_type == "spine.calendar.send_invite"))
        ).scalar_one()
        invoices = (await s.execute(select(Invoice))).scalars().all()
    assert kickoff is not None and kickoff.title.startswith("Kickoff:")
    assert invite.status == "pending"  # outward: waits for a person
    assert len(invoices) == 1  # Finance reacted to the same event, independently


async def test_milestone_billing_goes_to_finance_task_by_task(
    services: Services, founder: Any, business: dict[str, Any]
) -> None:
    bid = uuid.UUID(business["id"])
    _, deal = await _deal(founder, business, value=100_001, win=False)
    async with services.db.tenant(bid) as s:
        await s.execute(
            update(Deal).where(Deal.id == uuid.UUID(deal["id"])).values(attributes={"billing": "milestones"})
        )
    await founder.patch(url(business, f"/gtm/deals/{deal['id']}"), json={"stage_key": "advertiser"})
    await Worker(services).run_until_idle()
    async with services.db.tenant(bid) as s:
        assert (await s.execute(select(Invoice))).scalars().all() == []  # not invoiced up front
        tasks = (await s.execute(select(Task).order_by(Task.position))).scalars().all()
    billable = [t.billable_minor for t in tasks if t.milestone]
    assert billable == [50_000, 50_001] and sum(billable) == 100_001  # no cent lost to rounding

    for t in tasks:
        res = await founder.post(url(business, f"/ops/tasks/{t.id}/done"))
        assert res.status_code == 200
    await Worker(services).run_until_idle()
    async with services.db.tenant(bid) as s:
        totals = sorted(i.subtotal_minor for i in (await s.execute(select(Invoice))).scalars())
        project = (await s.execute(select(Project))).scalar_one()
    assert totals == [50_000, 50_001]
    assert project.status == "done"


async def test_tickets_are_answered_only_from_help_articles(
    services: Services, founder: Any, business: dict[str, Any]
) -> None:
    bid = uuid.UUID(business["id"])
    contact, _ = await _deal(founder, business, win=False)
    await founder.post(
        url(business, "/ops/articles"),
        json={
            "title": "Changing your ad dates",
            "body": "You can move your ad dates up to 48 hours before the slot starts. Write to us with the new dates.",
        },
    )
    t = (
        await founder.post(
            url(business, "/ops/tickets"),
            json={
                "subject": "Move my ad dates",
                "body": "Can I change the dates of my ad slot?",
                "contact_id": contact["id"],
            },
        )
    ).json()
    await Worker(services).run_until_idle()
    ticket = (await founder.get(url(business, f"/ops/tickets/{t['id']}"))).json()
    assert ticket["kind"] == "question" and ticket["reply_draft"].startswith(
        "Thanks for asking. You can move your ad dates"
    )
    before = len(services.sent_emails)  # type: ignore[attr-defined]
    assert (
        await founder.post(url(business, f"/decisions/{ticket['decision_id']}/approve"), json={})
    ).status_code == 200
    await Worker(services).run_until_idle()
    sent = [m for m in services.sent_emails[before:] if m["subject"] == "Re: Move my ad dates"]  # type: ignore[attr-defined]
    assert len(sent) == 1 and sent[0]["to"] == contact["email"]
    assert (await founder.get(url(business, f"/ops/tickets/{t['id']}"))).json()["status"] == "waiting"

    nothing = (
        await founder.post(
            url(business, "/ops/tickets"),
            json={"subject": "Hello", "body": "Do you sponsor podcasts?", "contact_id": contact["id"]},
        )
    ).json()
    feature = (
        await founder.post(
            url(business, "/ops/tickets"),
            json={"subject": "Idea", "body": "Would love a weekly report by email", "contact_id": contact["id"]},
        )
    ).json()
    await Worker(services).run_until_idle()
    assert (await founder.get(url(business, f"/ops/tickets/{nothing['id']}"))).json()["reply_draft"] is None
    async with services.db.tenant(bid) as s:
        fb = (await s.execute(select(LedgerEvent).where(LedgerEvent.type == "ops.feedback.received"))).scalars().all()
    assert [e.data["ticket_id"] for e in fb] == [feature["id"]]


async def test_unpaid_customer_projects_go_on_hold(services: Services, founder: Any, business: dict[str, Any]) -> None:
    bid = uuid.UUID(business["id"])
    await _deal(founder, business)
    await Worker(services).run_until_idle()
    from projectx.kernel.time import today_in
    from projectx.modules.finance import service as fin
    from projectx.spine.tenancy.models import Business

    async with services.db.tenant(bid) as s:
        inv = (await s.execute(select(Invoice))).scalar_one()
        biz = await s.get(Business, bid)
        assert biz is not None
        await fin.issue(s, biz, inv, actor=fin.AGENT)
        inv.due_date = today_in(biz.timezone) - timedelta(days=40)
    await fin.daily(services, bid)
    await Worker(services).run_until_idle()
    p = (await founder.get(url(business, "/ops/projects"))).json()["items"][0]
    assert p["status"] == "on_hold" and "overdue" in p["hold_reason"]


async def test_erasing_a_contact_erases_their_tickets(
    services: Services, founder: Any, business: dict[str, Any]
) -> None:
    bid = uuid.UUID(business["id"])
    contact, _ = await _deal(founder, business, win=False)
    t = (
        await founder.post(
            url(business, "/ops/tickets"),
            json={"subject": "My address", "body": "I live at 12 Lane", "contact_id": contact["id"]},
        )
    ).json()
    from projectx.modules.gtm.compliance.service import erase_contact
    from projectx.spine.ledger.service import Actor
    from projectx.spine.twin.models import Contact

    async with services.db.tenant(bid) as s:
        c = await s.get(Contact, uuid.UUID(contact["id"]))
        assert c is not None
        await erase_contact(s, c, actor=Actor.system("test"))
    await Worker(services).run_until_idle()
    async with services.db.tenant(bid) as s:
        ticket = await s.get(Ticket, uuid.UUID(t["id"]))
    assert ticket is not None and ticket.body == "[erased]" and ticket.subject == "[erased]"
