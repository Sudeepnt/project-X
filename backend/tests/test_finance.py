"""Finance working with the other departments: won deal → invoice → payment → books; collections; tax."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select, update

from projectx.kernel.time import today_in
from projectx.modules.finance import books
from projectx.modules.finance import service as fin
from projectx.modules.finance.models import Invoice, TaxFiling
from projectx.services import Services
from projectx.spine.bus import Handoff
from projectx.spine.decisions.models import Decision
from projectx.spine.jobs.worker import Worker
from projectx.spine.tenancy.models import Business


def gtm(business: dict[str, Any], path: str) -> str:
    return f"/api/v1/b/{business['id']}/gtm{path}"


def fin_url(business: dict[str, Any], path: str) -> str:
    return f"/api/v1/b/{business['id']}/finance{path}"


async def _won_deal(
    founder: Any, business: dict[str, Any], value: int = 100_000
) -> tuple[dict[str, Any], dict[str, Any]]:
    pipelines = (await founder.get(gtm(business, "/pipelines"))).json()["items"]
    advertisers = next(p for p in pipelines if p["key"] == "advertisers")
    contact = (
        await founder.post(
            gtm(business, "/contacts"),
            json={"email": f"brand-{uuid.uuid4().hex[:6]}@example.com", "name": "Acme Brand", "stage": "lead"},
        )
    ).json()
    deal = (
        await founder.post(
            gtm(business, "/deals"),
            json={
                "pipeline_id": advertisers["id"],
                "title": "Acme sponsorship",
                "contact_id": contact["id"],
                "value_minor": value,
            },
        )
    ).json()
    won = await founder.patch(gtm(business, f"/deals/{deal['id']}"), json={"stage_key": "advertiser"})
    assert won.json()["status"] == "won"
    return contact, deal


async def test_won_deal_is_invoiced_paid_and_booked_without_anyone_passing_notes(
    services: Services, founder: Any, business: dict[str, Any]
) -> None:
    bid = uuid.UUID(business["id"])
    await founder.put(fin_url(business, "/settings"), json={"tax_rate_bp": 1800})
    contact, _deal = await _won_deal(founder, business)
    await Worker(services).run_until_idle()  # the bus delivers "deal won" to Finance

    invoices = (await founder.get(fin_url(business, "/invoices"))).json()["items"]
    assert len(invoices) == 1
    inv = invoices[0]
    assert (inv["subtotal_minor"], inv["tax_minor"], inv["total_minor"], inv["status"]) == (
        100_000,
        18_000,
        118_000,
        "draft",
    )
    async with services.db.tenant(bid) as s:
        handoff = (
            await s.execute(select(Handoff).where(Handoff.subscriber == "finance.invoice_won_deal"))
        ).scalar_one()
        card = (await s.execute(select(Decision).where(Decision.action_type == "finance.send_invoice"))).scalar_one()
    assert (handoff.from_module, handoff.to_module, handoff.status) == ("gtm", "finance", "done")

    before = len(services.sent_emails)  # type: ignore[attr-defined]
    assert (await founder.post(f"/api/v1/b/{bid}/decisions/{card.id}/approve", json={})).status_code == 200
    await Worker(services).run_until_idle()
    inv = (await founder.get(fin_url(business, "/invoices"))).json()["items"][0]
    assert inv["status"] == "sent" and inv["due_date"]
    mails = [m for m in services.sent_emails[before:] if m["subject"].startswith("Invoice ")]  # type: ignore[attr-defined]
    assert len(mails) == 1 and mails[0]["to"] == contact["email"]

    from projectx.modules.gtm.crm.payments import record_payment

    async with services.db.tenant(bid) as s:
        biz = await s.get(Business, bid)
        assert biz is not None
        await record_payment(
            s,
            biz,
            provider="stripe",
            external_id="pi_a",
            amount_minor=118_000,
            currency=biz.currency,
            contact_id=uuid.UUID(contact["id"]),
        )
    await Worker(services).run_until_idle()
    inv = (await founder.get(fin_url(business, "/invoices"))).json()["items"][0]
    assert inv["status"] == "paid" and inv["paid_minor"] == 118_000

    sheet = (await founder.get(fin_url(business, "/reports/balance-sheet"))).json()
    assert sheet["balanced"] is True
    assert sheet["accounts"]["Cash and bank"] == 118_000
    assert sheet["accounts"]["Tax payable"] == 18_000
    assert "Accounts receivable" not in sheet["accounts"]  # fully collected
    pnl = (await founder.get(fin_url(business, "/reports/pnl"))).json()
    assert pnl["total_income_minor"] == 100_000

    # A second won deal gets its own invoice (one per deal, keyed by the deal).
    _, _again = await _won_deal(founder, business, value=5_000)
    await Worker(services).run_until_idle()
    assert len((await founder.get(fin_url(business, "/invoices"))).json()["items"]) == 2


async def test_overdue_invoices_are_chased_then_handed_to_legal(
    services: Services, founder: Any, business: dict[str, Any]
) -> None:
    bid = uuid.UUID(business["id"])
    async with services.db.tenant(bid) as s:
        biz = await s.get(Business, bid)
        assert biz is not None
        inv, _ = await fin.create_invoice(
            s, biz, source_key="t:1", lines=[{"description": "Ad slot", "unit_minor": 50_000}]
        )
        await fin.issue(s, biz, inv, actor=fin.AGENT)
        inv.due_date = today_in(biz.timezone) - timedelta(days=8)
        invoice_id = inv.id
    await fin.daily(services, bid)
    async with services.db.tenant(bid) as s:
        inv = await s.get(Invoice, invoice_id)
        reminders = (
            (await s.execute(select(Decision).where(Decision.action_type == "finance.send_reminder"))).scalars().all()
        )
    assert inv is not None and inv.status == "overdue" and inv.reminders_sent == [1, 7]
    assert len(reminders) == 1  # one reminder for the latest step, not one per missed step
    await fin.daily(services, bid)
    async with services.db.tenant(bid) as s:
        assert (
            len(
                (await s.execute(select(Decision).where(Decision.action_type == "finance.send_reminder")))
                .scalars()
                .all()
            )
            == 1
        )
        await s.execute(
            update(Invoice).where(Invoice.id == invoice_id).values(due_date=date.today() - timedelta(days=31))
        )
    await fin.daily(services, bid)
    async with services.db.tenant(bid) as s:
        inv = await s.get(Invoice, invoice_id)
        from projectx.spine.ledger.models import LedgerEvent

        types = (await s.execute(select(LedgerEvent.type).where(LedgerEvent.subject_id == invoice_id))).scalars().all()
    assert inv is not None and inv.escalated_at is not None
    assert "finance.invoice.escalated" in types


async def test_closed_tax_period_becomes_a_filing_task_and_then_a_payment(
    services: Services, founder: Any, business: dict[str, Any]
) -> None:
    bid = uuid.UUID(business["id"])
    async with services.db.tenant(bid) as s:
        biz = await s.get(Business, bid)
        assert biz is not None
        settings = await books.setup(s, biz)
        settings.tax_rate_bp = 1000
        inv, _ = await fin.create_invoice(
            s, biz, source_key="t:tax", lines=[{"description": "Work", "unit_minor": 20_000}]
        )
        await fin.issue(s, biz, inv, actor=fin.AGENT)
        last_month = today_in(biz.timezone).replace(day=1) - timedelta(days=3)
        from projectx.modules.finance.models import JournalEntry

        await s.execute(update(JournalEntry).values(entry_date=last_month))
        await s.execute(update(Invoice).where(Invoice.id == inv.id).values(issue_date=last_month))
    await fin.daily(services, bid)
    filings = (await founder.get(fin_url(business, "/tax"))).json()["items"]
    assert [(f["output_tax_minor"], f["status"]) for f in filings] == [(2_000, "ready")]
    async with services.db.tenant(bid) as s:
        task = (await s.execute(select(Decision).where(Decision.action_type == "finance.file_tax"))).scalar_one()
    assert task.kind == "manual_task"  # a person files; the system never does
    done = await founder.post(f"/api/v1/b/{bid}/decisions/{task.id}/approve", json={"result": {"reference": "ARN-123"}})
    assert done.status_code == 200, done.text
    await Worker(services).run_until_idle()
    async with services.db.tenant(bid) as s:
        filing = (await s.execute(select(TaxFiling))).scalar_one()
        pay = (await s.execute(select(Decision).where(Decision.action_type == "finance.pay_bill"))).scalar_one()
    assert filing.status == "filed" and filing.reference == "ARN-123"
    assert pay.status == "pending" and pay.cost_minor == 2_000  # money out always waits for a person
    await fin.daily(services, bid)
    assert len((await founder.get(fin_url(business, "/tax"))).json()["items"]) == 1  # never computed twice


def test_tax_rounding_and_periods() -> None:
    assert fin.tax_of(10_001, 1800) == 1800  # 1800.18 → 1800
    assert fin.tax_of(10_003, 1800) == 1801  # 1800.54 → 1801
    assert fin.period_bounds(date(2026, 11, 28), "monthly") == (date(2026, 11, 1), date(2026, 12, 1))
    assert fin.period_bounds(date(2026, 11, 28), "quarterly") == (date(2026, 10, 1), date(2027, 1, 1))
    assert fin.period_bounds(date(2026, 12, 31), "monthly") == (date(2026, 12, 1), date(2027, 1, 1))
