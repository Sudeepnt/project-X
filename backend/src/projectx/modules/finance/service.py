"""Finance at work: invoice what was sold, collect what is owed, pay what is due, prepare tax.

Runs by itself from other departments' events:
- a deal is won (GTM) → an invoice is drafted and proposed for sending,
- a delivery milestone is completed (Operations) → that milestone is invoiced,
- a payment arrives (GTM payments) → it is matched to the oldest open invoice and booked,
- payroll is approved (People), a subscription renews (Admin) → a bill is booked and its payment proposed,
- every day: overdue invoices get polite reminders, long-overdue ones go to Legal, finished tax periods are
  computed and handed to a person to file.

People decide only the one-way doors: paying money out and filing tax. Sending invoices and reminders can
earn the right to run alone through the normal autonomy ladder.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.errors import Conflict, NotFound, ValidationFailed
from projectx.kernel.money import format_money
from projectx.kernel.time import today_in, utcnow
from projectx.modules.finance import books
from projectx.modules.finance.models import Bill, FinanceSettings, Invoice, TaxFiling
from projectx.services import Services
from projectx.spine.bus import BusContext, on
from projectx.spine.decisions import service as decisions
from projectx.spine.decisions.executors import ExecutionContext, ExecutionResult, executor, undoer
from projectx.spine.ledger.events import EventData, register_event
from projectx.spine.ledger.service import Actor, append
from projectx.spine.lifecycle import queue_email
from projectx.spine.policy.actions import ActionType, register_action
from projectx.spine.policy.levels import Level
from projectx.spine.tenancy.models import Business
from projectx.spine.twin.models import Contact, Deal

M = "finance"
AGENT = Actor.agent("finance")

SEND_INVOICE = register_action(
    ActionType(
        key="finance.send_invoice",
        module=M,
        pack="finance.billing",
        title="Send an invoice",
        description="Email an invoice to the customer and book it as receivable.",
        default_level=Level.ASK_FIRST,
        max_level=Level.RUN,
        reversible=True,
        undo="Void the invoice and email the customer a correction.",
    )
)
SEND_REMINDER = register_action(
    ActionType(
        key="finance.send_reminder",
        module=M,
        pack="finance.collections",
        title="Remind a customer to pay",
        description="Email a polite reminder about an overdue invoice.",
        default_level=Level.RUN_AND_TELL,
        max_level=Level.RUN,
        reversible=True,
        undo="Emails can't be recalled: send an apology if it was a mistake.",
    )
)
PAY_BILL = register_action(
    ActionType(
        key="finance.pay_bill",
        module=M,
        pack="finance.payables",
        title="Pay a bill",
        description="Money out: pay a supplier, payroll or tax bill.",
        default_level=Level.ASK_FIRST,
        max_level=Level.ASK_FIRST,
        reversible=False,
        moves_money=True,
    )
)
FILE_TAX = register_action(
    ActionType(
        key="finance.file_tax",
        module=M,
        pack="finance.tax",
        title="File a tax return",
        description="A person files the prepared return with the tax authority and records the reference.",
        default_level=Level.DRAFT,
        max_level=Level.DRAFT,
        reversible=False,
    )
)


class InvoiceChanged(EventData):
    invoice_id: str
    status: str
    total_minor: int
    currency: str
    contact_id: str | None = None
    deal_id: str | None = None


class BillChanged(EventData):
    bill_id: str
    status: str
    amount_minor: int
    currency: str
    category: str


class TaxChanged(EventData):
    filing_id: str
    status: str
    output_tax_minor: int


for _key, _desc, _schema in (
    ("finance.invoice.created", "An invoice was drafted", InvoiceChanged),
    ("finance.invoice.sent", "An invoice was sent to the customer", InvoiceChanged),
    ("finance.invoice.paid", "An invoice was paid in full", InvoiceChanged),
    ("finance.invoice.overdue", "An invoice went past its due date", InvoiceChanged),
    ("finance.invoice.escalated", "A long-overdue invoice was handed to Legal", InvoiceChanged),
    ("finance.invoice.voided", "An invoice was voided", InvoiceChanged),
    ("finance.bill.created", "A bill was booked", BillChanged),
    ("finance.bill.paid", "A bill was paid", BillChanged),
    ("finance.tax.ready", "A tax period was computed and is ready to file", TaxChanged),
    ("finance.tax.filed", "A tax return was filed", TaxChanged),
):
    register_event(_key, _desc, _schema)


def _inv_data(i: Invoice) -> dict[str, Any]:
    return {
        "invoice_id": str(i.id),
        "status": i.status,
        "total_minor": i.total_minor,
        "currency": i.currency,
        "contact_id": str(i.contact_id) if i.contact_id else None,
        "deal_id": str(i.deal_id) if i.deal_id else None,
    }


async def _event(
    session: AsyncSession, business_id: uuid.UUID, type_: str, inv: Invoice, actor: Actor, **cause: Any
) -> None:
    await append(
        session,
        business_id=business_id,
        type=type_,
        actor=actor,
        subject=("invoice", inv.id),
        data=_inv_data(inv),
        **cause,
    )


def tax_of(amount_minor: int, rate_bp: int) -> int:
    """Half-up rounding to the minor unit, in integers (no floats anywhere near money)."""
    return (amount_minor * rate_bp + 5000) // 10000


# ---------------------------------------------------------------- invoices


async def create_invoice(
    session: AsyncSession,
    business: Business,
    *,
    source_key: str,
    lines: list[dict[str, Any]],
    currency: str | None = None,
    contact_id: uuid.UUID | None = None,
    deal_id: uuid.UUID | None = None,
    notes: str = "",
    actor: Actor = AGENT,
    cause: dict[str, Any] | None = None,
) -> tuple[Invoice, bool]:
    """Draft an invoice once per source. Returns (invoice, created)."""
    existing = await session.scalar(select(Invoice).where(Invoice.source_key == source_key))
    if existing is not None:
        return existing, False
    settings = await books.setup(session, business)
    clean: list[dict[str, Any]] = []
    for line in lines:
        qty, unit = int(line.get("quantity", 1)), int(line["unit_minor"])
        if qty <= 0 or unit < 0:
            raise ValidationFailed("Quantities are positive and prices are not negative")
        clean.append(
            {
                "description": str(line["description"])[:300],
                "quantity": qty,
                "unit_minor": unit,
                "amount_minor": qty * unit,
            }
        )
    subtotal = sum(line["amount_minor"] for line in clean)
    if subtotal <= 0:
        raise ValidationFailed("An invoice needs a positive amount")
    await session.refresh(settings, with_for_update=True)  # one number at a time: no two invoices share one
    tax = tax_of(subtotal, settings.tax_rate_bp)
    number = f"{settings.invoice_prefix}{settings.next_invoice_no:05d}"
    settings.next_invoice_no += 1
    inv = Invoice(
        business_id=business.id,
        number=number,
        contact_id=contact_id,
        deal_id=deal_id,
        source_key=source_key[:120],
        currency=currency or business.currency,
        lines=clean,
        subtotal_minor=subtotal,
        tax_rate_bp=settings.tax_rate_bp,
        tax_minor=tax,
        total_minor=subtotal + tax,
        notes=notes,
    )
    session.add(inv)
    await session.flush()
    await _event(session, business.id, "finance.invoice.created", inv, actor, **(cause or {}))
    return inv, True


async def propose_send(session: AsyncSession, business: Business, inv: Invoice, *, why: str) -> uuid.UUID | None:
    contact = await session.get(Contact, inv.contact_id) if inv.contact_id else None
    who = (contact.name or contact.company or "the customer") if contact else "the customer"
    outcome = await decisions.propose(
        session,
        business.id,
        decisions.Proposal(
            action_type=SEND_INVOICE.key,
            title=f"Send invoice {inv.number} to {who} for {format_money(inv.total_minor, inv.currency)}",
            summary=f"{len(inv.lines)} line(s), {format_money(inv.tax_minor, inv.currency)} tax.",
            payload={"invoice_id": str(inv.id)},
            preview={"invoice": invoice_view(inv)},
            why_now=why,
            if_no="The invoice stays a draft; nothing is sent or booked.",
            subject=("invoice", inv.id),
            requested_by="pack:finance.billing",
            priority=30,
        ),
    )
    return outcome.decision_id


async def issue(
    session: AsyncSession, business: Business, inv: Invoice, *, actor: Actor, pay_link: str | None = None
) -> None:
    """Book the invoice (receivable, revenue, tax) and queue the email. Safe to call twice."""
    if inv.status != "draft":
        return
    settings = await books.setup(session, business)
    today = today_in(business.timezone)
    inv.issue_date, inv.due_date = today, today + timedelta(days=settings.payment_terms_days)
    inv.status = "sent"
    await books.post(
        session,
        business.id,
        entry_date=today,
        memo=f"Invoice {inv.number}",
        source=("invoice", inv.id, "issue"),
        currency=inv.currency,
        lines=[(books.AR, inv.total_minor, 0), (books.REVENUE, 0, inv.subtotal_minor), (books.TAX, 0, inv.tax_minor)],
    )
    contact = await session.get(Contact, inv.contact_id) if inv.contact_id else None
    if contact and contact.email and not contact.erased_at:
        text, html = _invoice_email(business, inv, contact.name, pay_link)
        await queue_email(
            session,
            to=contact.email,
            subject=f"Invoice {inv.number} from {business.name}",
            html=html,
            text=text,
            business_id=business.id,
        )
    await _event(session, business.id, "finance.invoice.sent", inv, actor)


def _invoice_email(business: Business, inv: Invoice, name: str | None, pay_link: str | None = None) -> tuple[str, str]:
    rows = [
        f"{line['description']} x {line['quantity']}: {format_money(line['amount_minor'], inv.currency)}"
        for line in inv.lines
    ]
    text = (
        f"Hello{(' ' + name) if name else ''},\n\nInvoice {inv.number} from {business.name}.\n\n"
        + "\n".join(rows)
        + f"\nSubtotal: {format_money(inv.subtotal_minor, inv.currency)}"
        + f"\nTax: {format_money(inv.tax_minor, inv.currency)}"
        + f"\nTotal due by {inv.due_date}: {format_money(inv.total_minor, inv.currency)}"
        + (f"\n\nPay online: {pay_link}" if pay_link else "")
        + "\n\nThank you."
    )
    html = "<p>" + "<br>".join(line.replace("&", "&amp;").replace("<", "&lt;") for line in text.split("\n")) + "</p>"
    return text, html


@executor(SEND_INVOICE.key)
async def _send_invoice(ctx: ExecutionContext) -> ExecutionResult:
    async with ctx.services.db.tenant(ctx.business_id) as session:
        inv = await session.get(Invoice, uuid.UUID(ctx.payload["invoice_id"]), with_for_update=True)
        business = await session.get(Business, ctx.business_id)
        if inv is None or business is None:
            raise NotFound("Invoice not found")
        from projectx.modules.finance.payments import pay_url
        from projectx.spine.integrations.service import is_ready

        link = (
            pay_url(ctx.services, ctx.business_id, inv.id)
            if await is_ready(ctx.services, ctx.business_id, "stripe")
            else None
        )
        await issue(session, business, inv, actor=AGENT, pay_link=link)
        return ExecutionResult(
            data={"invoice_id": str(inv.id), "number": inv.number},
            undo_payload={"invoice_id": str(inv.id)},
            summary=f"Sent {inv.number}.",
        )


@undoer(SEND_INVOICE.key)
async def _void_invoice(ctx: ExecutionContext) -> dict[str, Any]:
    async with ctx.services.db.tenant(ctx.business_id) as session:
        inv = await session.get(Invoice, uuid.UUID(ctx.payload["invoice_id"]), with_for_update=True)
        business = await session.get(Business, ctx.business_id)
        assert inv is not None
        assert business is not None
        await void(session, business, inv, actor=AGENT)
        return {"voided": inv.number}


async def void(session: AsyncSession, business: Business, inv: Invoice, *, actor: Actor) -> None:
    if inv.paid_minor:
        raise Conflict("Money was already received against this invoice; refund it instead of voiding")
    if inv.status in ("sent", "overdue"):
        await books.post(
            session,
            business.id,
            entry_date=today_in(business.timezone),
            memo=f"Void invoice {inv.number}",
            source=("invoice", inv.id, "void"),
            currency=inv.currency,
            lines=[
                (books.AR, 0, inv.total_minor),
                (books.REVENUE, inv.subtotal_minor, 0),
                (books.TAX, inv.tax_minor, 0),
            ],
        )
    inv.status = "void"
    await _event(session, business.id, "finance.invoice.voided", inv, actor)


async def apply_payment(
    session: AsyncSession,
    business: Business,
    *,
    payment_id: uuid.UUID,
    contact_id: uuid.UUID | None,
    amount_minor: int,
    currency: str,
    cause: dict[str, Any],
    invoice_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Match money received to the oldest open invoices of the same customer; book any remainder as a cash sale."""
    await books.setup(session, business)
    today = today_in(business.timezone)
    left = amount_minor
    applied: list[str] = []
    open_states = ("sent", "partially_paid", "overdue")
    candidates: list[Invoice] = []
    if invoice_id is not None:  # paid through that invoice's own link: it comes first, whoever paid
        target = await session.get(Invoice, invoice_id, with_for_update=True)
        if target is not None and target.status in open_states and target.currency == currency:
            candidates.append(target)
    if contact_id is not None:
        candidates += [
            i
            for i in (
                await session.execute(
                    select(Invoice)
                    .where(
                        Invoice.contact_id == contact_id, Invoice.currency == currency, Invoice.status.in_(open_states)
                    )
                    .order_by(Invoice.due_date, Invoice.created_at)
                    .with_for_update()
                )
            ).scalars()
            if i not in candidates
        ]
    if candidates:
        for inv in candidates:
            if left <= 0:
                break
            take = min(left, inv.total_minor - inv.paid_minor)
            await books.post(
                session,
                business.id,
                entry_date=today,
                memo=f"Payment for {inv.number}",
                source=("payment", payment_id, f"inv:{inv.number}"),
                currency=currency,
                lines=[(books.CASH, take, 0), (books.AR, 0, take)],
            )
            inv.paid_minor += take
            left -= take
            applied.append(inv.number)
            if inv.paid_minor >= inv.total_minor:
                inv.status, inv.paid_at = "paid", utcnow()
                await _event(session, business.id, "finance.invoice.paid", inv, AGENT, **cause)
            else:
                inv.status = "partially_paid"
    if left > 0:
        settings = await books.setup(session, business)
        net = left * 10000 // (10000 + settings.tax_rate_bp)  # the remainder is a cash sale, tax included
        await books.post(
            session,
            business.id,
            entry_date=today,
            memo="Cash sale",
            source=("payment", payment_id, "sale"),
            currency=currency,
            lines=[(books.CASH, left, 0), (books.REVENUE, 0, net), (books.TAX, 0, left - net)],
        )
    return {"applied_to": applied, "unapplied_minor": left}


# ---------------------------------------------------------------- reactions to other departments


@on(
    "gtm.deal.changed",
    "finance.invoice_won_deal",
    when=lambda d: d.get("status") == "won" and d.get("value_minor", 0) > 0,
)
async def on_deal_won(ctx: BusContext) -> dict[str, Any] | None:
    async with ctx.services.db.tenant(ctx.business_id) as session:
        deal = await session.get(Deal, uuid.UUID(ctx.data["deal_id"]))
        business = await session.get(Business, ctx.business_id)
        if deal is None or business is None:
            return {"skipped": "deal gone"}
        if (deal.attributes or {}).get("billing") == "milestones":
            return {"skipped": "billed by milestone"}
        inv, created = await create_invoice(
            session,
            business,
            source_key=f"deal:{deal.id}",
            lines=[{"description": deal.title, "quantity": 1, "unit_minor": deal.value_minor}],
            currency=deal.currency,
            contact_id=deal.contact_id,
            deal_id=deal.id,
            cause=ctx.cause,
        )
        if not created:
            return {"skipped": "already invoiced", "invoice": inv.number}
        decision_id = await propose_send(session, business, inv, why="The deal was just won.")
        return {"invoice": inv.number, "decision_id": decision_id}


@on("ops.milestone.completed", "finance.invoice_milestone", when=lambda d: d.get("billable_minor", 0) > 0)
async def on_milestone(ctx: BusContext) -> dict[str, Any] | None:
    async with ctx.services.db.tenant(ctx.business_id) as session:
        business = await session.get(Business, ctx.business_id)
        assert business is not None
        inv, created = await create_invoice(
            session,
            business,
            source_key=f"milestone:{ctx.data['task_id']}",
            lines=[
                {
                    "description": ctx.data.get("label", "Milestone"),
                    "quantity": 1,
                    "unit_minor": ctx.data["billable_minor"],
                }
            ],
            currency=ctx.data.get("currency") or business.currency,
            contact_id=uuid.UUID(ctx.data["contact_id"]) if ctx.data.get("contact_id") else None,
            cause=ctx.cause,
        )
        if not created:
            return {"skipped": "already invoiced"}
        await propose_send(session, business, inv, why="A billable milestone was delivered.")
        return {"invoice": inv.number}


@on("gtm.payment.recorded", "finance.book_payment")
async def on_payment(ctx: BusContext) -> dict[str, Any] | None:
    from projectx.spine.twin.models import Payment

    async with ctx.services.db.tenant(ctx.business_id) as session:
        business = await session.get(Business, ctx.business_id)
        assert business is not None
        payment = await session.get(Payment, uuid.UUID(ctx.data["payment_id"]))
        target = (payment.meta or {}).get("invoice_id") if payment else None
        return await apply_payment(
            session,
            business,
            invoice_id=uuid.UUID(target) if target else None,
            payment_id=uuid.UUID(ctx.data["payment_id"]),
            contact_id=uuid.UUID(ctx.data["contact_id"]) if ctx.data.get("contact_id") else None,
            amount_minor=int(ctx.data["amount_minor"]),
            currency=ctx.data["currency"],
            cause=ctx.cause,
        )


async def book_bill(
    session: AsyncSession,
    business: Business,
    *,
    source_key: str,
    supplier: str,
    category: str,
    amount_minor: int,
    currency: str,
    due_date: date,
    notes: str = "",
    cause: dict[str, Any] | None = None,
    expense_account: str | None = None,
) -> tuple[Bill, bool]:
    existing = await session.scalar(select(Bill).where(Bill.source_key == source_key))
    if existing is not None:
        return existing, False
    await books.setup(session, business)
    bill = Bill(
        business_id=business.id,
        supplier=supplier[:200],
        category=category,
        source_key=source_key[:120],
        currency=currency,
        amount_minor=amount_minor,
        due_date=due_date,
        notes=notes,
    )
    session.add(bill)
    await session.flush()
    debit = expense_account or {"software": books.SOFTWARE, "cost": books.COST}.get(category, books.OTHER)
    if category in ("payroll", "tax"):
        debit = ""  # already booked by the department that created the liability; the bill is just the payment
    else:
        await books.post(
            session,
            business.id,
            entry_date=today_in(business.timezone),
            memo=f"Bill: {supplier}",
            source=("bill", bill.id, "book"),
            currency=currency,
            lines=[(debit, amount_minor, 0), (books.AP, 0, amount_minor)],
        )
    await append(
        session,
        business_id=business.id,
        type="finance.bill.created",
        actor=AGENT,
        subject=("bill", bill.id),
        data={
            "bill_id": str(bill.id),
            "status": bill.status,
            "amount_minor": amount_minor,
            "currency": currency,
            "category": category,
        },
        **(cause or {}),
    )
    return bill, True


async def propose_payment(session: AsyncSession, business: Business, bill: Bill, *, why: str) -> uuid.UUID | None:
    outcome = await decisions.propose(
        session,
        business.id,
        decisions.Proposal(
            action_type=PAY_BILL.key,
            title=f"Pay {bill.supplier}: {format_money(bill.amount_minor, bill.currency)} by {bill.due_date:%d %b}",
            summary=bill.notes[:300],
            payload={"bill_id": str(bill.id)},
            amount_minor=bill.amount_minor,
            currency=bill.currency,
            why_now=why,
            if_no="The bill stays unpaid; it will come back as it gets close to its due date.",
            subject=("bill", bill.id),
            requested_by="pack:finance.payables",
            priority=20,
        ),
    )
    bill.decision_id = outcome.decision_id
    return outcome.decision_id


@executor(PAY_BILL.key)
async def _pay_bill(ctx: ExecutionContext) -> ExecutionResult:
    async with ctx.services.db.tenant(ctx.business_id) as session:
        bill = await session.get(Bill, uuid.UUID(ctx.payload["bill_id"]), with_for_update=True)
        business = await session.get(Business, ctx.business_id)
        if bill is None or business is None:
            raise NotFound("Bill not found")
        if bill.status == "paid":
            return ExecutionResult(data={"bill_id": str(bill.id), "already": True})
        credit_side = {"payroll": books.PAYROLL_LIAB, "tax": books.TAX}.get(bill.category, books.AP)
        await books.post(
            session,
            business.id,
            entry_date=today_in(business.timezone),
            memo=f"Paid {bill.supplier}",
            source=("bill", bill.id, "pay"),
            currency=bill.currency,
            lines=[(credit_side, bill.amount_minor, 0), (books.CASH, 0, bill.amount_minor)],
        )
        bill.status, bill.paid_at = "paid", utcnow()
        await append(
            session,
            business_id=business.id,
            type="finance.bill.paid",
            actor=AGENT,
            subject=("bill", bill.id),
            data={
                "bill_id": str(bill.id),
                "status": "paid",
                "amount_minor": bill.amount_minor,
                "currency": bill.currency,
                "category": bill.category,
            },
        )
        return ExecutionResult(
            data={"bill_id": str(bill.id)},
            amount_minor=bill.amount_minor,
            currency=bill.currency,
            summary=f"Paid {bill.supplier}.",
        )


@on("people.payroll.approved", "finance.book_payroll")
async def on_payroll(ctx: BusContext) -> dict[str, Any] | None:
    async with ctx.services.db.tenant(ctx.business_id) as session:
        business = await session.get(Business, ctx.business_id)
        assert business is not None
        await books.setup(session, business)
        run_id = uuid.UUID(ctx.data["run_id"])
        gross, net, withheld = int(ctx.data["gross_minor"]), int(ctx.data["net_minor"]), int(ctx.data["withheld_minor"])
        currency = ctx.data["currency"]
        await books.post(
            session,
            business.id,
            entry_date=today_in(business.timezone),
            memo=f"Payroll {ctx.data['period']}",
            source=("payroll", run_id, "accrue"),
            currency=currency,
            lines=[(books.SALARIES, gross, 0), (books.PAYROLL_LIAB, 0, net), (books.TAX, 0, withheld)],
        )
        bill, _ = await book_bill(
            session,
            business,
            source_key=f"payroll:{run_id}",
            supplier=f"Payroll {ctx.data['period']}",
            category="payroll",
            amount_minor=net,
            currency=currency,
            due_date=date.fromisoformat(ctx.data["pay_date"]),
            notes=f"{ctx.data['employees']} people. Withheld for tax: {format_money(withheld, currency)}.",
            cause=ctx.cause,
        )
        if bill.status == "open" and bill.decision_id is None:
            await propose_payment(session, business, bill, why="Payroll was approved; salaries are due on pay day.")
        return {"bill_id": str(bill.id)}


@on("admin.asset.renewing", "finance.book_renewal", when=lambda d: d.get("cost_minor", 0) > 0)
async def on_renewal(ctx: BusContext) -> dict[str, Any] | None:
    async with ctx.services.db.tenant(ctx.business_id) as session:
        business = await session.get(Business, ctx.business_id)
        assert business is not None
        bill, created = await book_bill(
            session,
            business,
            source_key=f"renewal:{ctx.data['asset_id']}:{ctx.data['renews_on']}",
            supplier=ctx.data.get("vendor") or "Subscription",
            category="software",
            amount_minor=int(ctx.data["cost_minor"]),
            currency=ctx.data.get("currency") or business.currency,
            due_date=date.fromisoformat(ctx.data["renews_on"]),
            notes=f"Renewal of {ctx.data.get('label', 'a subscription')}.",
            cause=ctx.cause,
        )
        if created:
            await propose_payment(session, business, bill, why="It renews soon; paying on time avoids losing access.")
        return {"bill_id": str(bill.id), "created": created}


# ---------------------------------------------------------------- the daily round: collections and tax


async def daily(services: Services, business_id: uuid.UUID) -> None:
    async with services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        if business is None:
            return
        settings = await books.setup(session, business)
        today = today_in(business.timezone)
        await _collections(session, business, settings, today)
        await _tax(session, business, settings, today)


async def _collections(session: AsyncSession, business: Business, settings: FinanceSettings, today: date) -> None:
    overdue = (
        await session.execute(
            select(Invoice)
            .where(Invoice.status.in_(("sent", "partially_paid", "overdue")), Invoice.due_date < today)
            .with_for_update()
        )
    ).scalars()
    for inv in overdue:
        assert inv.due_date is not None
        late = (today - inv.due_date).days
        if inv.status == "sent":
            inv.status = "overdue"
            await _event(session, business.id, "finance.invoice.overdue", inv, AGENT)
        if late >= settings.escalate_after_days and inv.escalated_at is None:
            inv.escalated_at = utcnow()
            await _event(session, business.id, "finance.invoice.escalated", inv, AGENT)
            continue
        due_steps = [d for d in settings.dunning_days if late >= d and d not in inv.reminders_sent]
        if due_steps and inv.escalated_at is None:
            step = max(due_steps)
            inv.reminders_sent = sorted({*inv.reminders_sent, *due_steps})
            owed = format_money(inv.total_minor - inv.paid_minor, inv.currency)
            await decisions.propose(
                session,
                business.id,
                decisions.Proposal(
                    action_type=SEND_REMINDER.key,
                    title=f"Remind about {inv.number}: {owed}, {late} day{'s' if late != 1 else ''} late",
                    payload={"invoice_id": str(inv.id), "step": step},
                    why_now=f"Reminder step {step} of {len(settings.dunning_days)}.",
                    subject=("invoice", inv.id),
                    requested_by="pack:finance.collections",
                    priority=40,
                ),
            )


@executor(SEND_REMINDER.key)
async def _send_reminder(ctx: ExecutionContext) -> ExecutionResult:
    async with ctx.services.db.tenant(ctx.business_id) as session:
        inv = await session.get(Invoice, uuid.UUID(ctx.payload["invoice_id"]))
        business = await session.get(Business, ctx.business_id)
        if inv is None or business is None:
            raise NotFound("Invoice not found")
        if inv.status == "paid":
            return ExecutionResult(data={"skipped": "paid meanwhile"})
        contact = await session.get(Contact, inv.contact_id) if inv.contact_id else None
        if not (contact and contact.email and not contact.erased_at):
            return ExecutionResult(data={"skipped": "no email for the customer"})
        owed = format_money(inv.total_minor - inv.paid_minor, inv.currency)
        text = (
            f"Hello{(' ' + contact.name) if contact.name else ''},\n\nA friendly reminder that invoice {inv.number} "
            f"for {owed} was due on {inv.due_date}. If you've already paid, thank you, and please ignore this.\n\n"
            f"{business.name}"
        )
        from projectx.modules.finance.payments import pay_url
        from projectx.spine.integrations.service import is_ready

        if await is_ready(ctx.services, ctx.business_id, "stripe"):
            text += f"\n\nPay online: {pay_url(ctx.services, ctx.business_id, inv.id)}"
        await queue_email(
            session,
            to=contact.email,
            subject=f"Reminder: invoice {inv.number}",
            html="<p>" + text.replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br>") + "</p>",
            text=text,
            business_id=business.id,
        )
        return ExecutionResult(
            data={"invoice": inv.number, "step": ctx.payload.get("step")}, summary=f"Reminded about {inv.number}."
        )


def period_bounds(day: date, period: str) -> tuple[date, date]:
    """The tax period containing `day`: [start, end)."""
    if period == "quarterly":
        start = date(day.year, ((day.month - 1) // 3) * 3 + 1, 1)
        months = 3
    else:
        start, months = day.replace(day=1), 1
    y, m = divmod(start.month - 1 + months, 12)
    return start, date(start.year + y, m + 1, 1)


async def _tax(session: AsyncSession, business: Business, settings: FinanceSettings, today: date) -> None:
    """When a period has closed, compute the tax collected in it and hand the return to a person."""
    current_start, _ = period_bounds(today, settings.tax_period)
    start, end = period_bounds(current_start - timedelta(days=1), settings.tax_period)
    if await session.scalar(select(TaxFiling.id).where(TaxFiling.period_start == start)):
        return
    if not await session.scalar(
        select(func.count()).select_from(Invoice).where(Invoice.issue_date >= start, Invoice.issue_date < end)
    ):
        has_books = (await books.balances(session, start=start, end=end)).get(books.TAX, 0)
        if not has_books:
            return
    output_tax = (await books.balances(session, start=start, end=end)).get(books.TAX, 0)
    due = date(end.year, end.month, min(settings.tax_due_day, 28))
    filing = TaxFiling(
        business_id=business.id,
        period_start=start,
        period_end=end - timedelta(days=1),
        due_date=due,
        tax_name=settings.tax_name,
        output_tax_minor=output_tax,
        currency=business.currency,
    )
    session.add(filing)
    await session.flush()
    period = f"{start:%b %Y}" if settings.tax_period == "monthly" else f"{start:%b}-{(end - timedelta(days=1)):%b %Y}"
    outcome = await decisions.propose(
        session,
        business.id,
        decisions.Proposal(
            action_type=FILE_TAX.key,
            title=f"File the {settings.tax_name} return for {period}: "
            f"{format_money(output_tax, business.currency)} by {due:%d %b}",
            summary="Computed from the books. A person files it and enters the reference.",
            payload={"filing_id": str(filing.id)},
            preview={"filing": filing_view(filing)},
            why_now="The period has closed.",
            if_no="Late filing usually means penalties and interest.",
            subject=("tax_filing", filing.id),
            requested_by="pack:finance.tax",
            priority=15,
        ),
    )
    filing.decision_id = outcome.decision_id
    from projectx.spine.calendar.service import add_reminder

    await add_reminder(
        session,
        business,
        title=f"{settings.tax_name} return for {period} is due",
        due_at=_at_nine(due, business.timezone),
        kind="deadline",
        importance="high",
        all_day=True,
        notify_before_minutes=[7 * 1440, 2 * 1440, 0],
        source="agent",
        dedupe_key=f"tax:{filing.id}",
        actor=AGENT,
    )
    await append(
        session,
        business_id=business.id,
        type="finance.tax.ready",
        actor=AGENT,
        subject=("tax_filing", filing.id),
        data={"filing_id": str(filing.id), "status": "ready", "output_tax_minor": output_tax},
    )


def _at_nine(day: date, tz: str) -> Any:
    from datetime import datetime, time

    from projectx.kernel.time import zone

    return datetime.combine(day, time(9), tzinfo=zone(tz))


async def tax_filed(services: Services, business_id: uuid.UUID, decision: Any) -> None:
    """A person filed the return (manual task done): record it and set up paying the tax due."""
    if decision.action_type != FILE_TAX.key:
        return
    async with services.db.tenant(business_id) as session:
        filing = await session.get(TaxFiling, uuid.UUID(decision.payload["filing_id"]), with_for_update=True)
        business = await session.get(Business, business_id)
        if filing is None or business is None or filing.status == "filed":
            return
        filing.status, filing.filed_at = "filed", utcnow()
        filing.reference = str((decision.result or {}).get("reference") or "")[:100] or None
        await append(
            session,
            business_id=business_id,
            type="finance.tax.filed",
            actor=Actor.user(decision.decided_by) if decision.decided_by else AGENT,
            subject=("tax_filing", filing.id),
            data={"filing_id": str(filing.id), "status": "filed", "output_tax_minor": filing.output_tax_minor},
        )
        if filing.output_tax_minor > 0:
            bill, _ = await book_bill(
                session,
                business,
                source_key=f"tax:{filing.id}",
                supplier=f"{filing.tax_name} {filing.period_start:%b %Y}",
                category="tax",
                amount_minor=filing.output_tax_minor,
                currency=filing.currency,
                due_date=filing.due_date,
            )
            await propose_payment(
                session, business, bill, why="The return is filed; the tax is due by the filing date."
            )


# ---------------------------------------------------------------- views and numbers


def invoice_view(i: Invoice) -> dict[str, Any]:
    return {
        "id": str(i.id),
        "number": i.number,
        "contact_id": str(i.contact_id) if i.contact_id else None,
        "deal_id": str(i.deal_id) if i.deal_id else None,
        "currency": i.currency,
        "lines": i.lines,
        "subtotal_minor": i.subtotal_minor,
        "tax_rate_bp": i.tax_rate_bp,
        "tax_minor": i.tax_minor,
        "total_minor": i.total_minor,
        "paid_minor": i.paid_minor,
        "issue_date": i.issue_date.isoformat() if i.issue_date else None,
        "due_date": i.due_date.isoformat() if i.due_date else None,
        "status": i.status,
        "reminders_sent": i.reminders_sent,
        "escalated": i.escalated_at is not None,
    }


def bill_view(b: Bill) -> dict[str, Any]:
    return {
        "id": str(b.id),
        "supplier": b.supplier,
        "category": b.category,
        "currency": b.currency,
        "amount_minor": b.amount_minor,
        "due_date": b.due_date.isoformat(),
        "status": b.status,
        "decision_id": str(b.decision_id) if b.decision_id else None,
    }


def filing_view(f: TaxFiling) -> dict[str, Any]:
    return {
        "id": str(f.id),
        "period_start": f.period_start.isoformat(),
        "period_end": f.period_end.isoformat(),
        "due_date": f.due_date.isoformat(),
        "tax_name": f.tax_name,
        "output_tax_minor": f.output_tax_minor,
        "currency": f.currency,
        "status": f.status,
        "reference": f.reference,
    }


async def numbers(session: AsyncSession, business_id: uuid.UUID) -> dict[str, Any]:
    business = await session.get(Business, business_id)
    if business is None or await session.get(FinanceSettings, business_id) is None:
        return {}
    today = today_in(business.timezone)
    month = await books.profit_and_loss(session, today.replace(day=1), today + timedelta(days=1))
    overdue = await session.scalar(
        select(func.coalesce(func.sum(Invoice.total_minor - Invoice.paid_minor), 0)).where(Invoice.status == "overdue")
    )
    run = await books.runway(session, today)
    return {
        "revenue_mtd_minor": month["total_income_minor"],
        "net_mtd_minor": month["net_minor"],
        "receivables_overdue_minor": int(overdue or 0),
        "cash_minor": run["cash_minor"],
        "runway_months": run["months"],
        "currency": business.currency,
    }
