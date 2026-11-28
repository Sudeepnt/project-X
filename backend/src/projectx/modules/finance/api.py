"""Finance endpoints: invoices, bills, tax filings, reports and settings."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from projectx.api.deps import BusinessDep, EditorDep, OwnerDep
from projectx.kernel.errors import NotFound, ValidationFailed
from projectx.kernel.time import today_in
from projectx.modules.finance import books
from projectx.modules.finance import service as fin
from projectx.modules.finance.models import Bill, Invoice, TaxFiling
from projectx.modules.finance.payments import CustomerSubscription, Plan, cancel_subscription, pay_url, subscribe_url
from projectx.spine.ledger.service import Actor

router = APIRouter(prefix="/api/v1/b/{business_id}/finance", tags=["finance"])


class LineIn(BaseModel):
    description: str = Field(min_length=1, max_length=300)
    quantity: int = Field(default=1, ge=1, le=100_000)
    unit_minor: int = Field(ge=0)


class InvoiceIn(BaseModel):
    lines: list[LineIn] = Field(min_length=1, max_length=100)
    contact_id: uuid.UUID | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    notes: str = Field(default="", max_length=2000)


class BillIn(BaseModel):
    supplier: str = Field(min_length=1, max_length=200)
    category: str = Field(default="other", pattern="^(software|cost|rent|other)$")
    amount_minor: int = Field(gt=0)
    due_date: date
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    notes: str = Field(default="", max_length=2000)


class SettingsIn(BaseModel):
    invoice_prefix: str | None = Field(default=None, max_length=12)
    payment_terms_days: int | None = Field(default=None, ge=0, le=180)
    tax_name: str | None = Field(default=None, max_length=20)
    tax_rate_bp: int | None = Field(default=None, ge=0, le=5000)
    tax_period: str | None = Field(default=None, pattern="^(monthly|quarterly)$")
    tax_due_day: int | None = Field(default=None, ge=1, le=28)
    dunning_days: list[int] | None = Field(default=None, max_length=6)
    escalate_after_days: int | None = Field(default=None, ge=7, le=365)


@router.get("/settings")
async def get_settings(ctx: BusinessDep) -> dict[str, Any]:
    s = await books.setup(ctx.session, ctx.business)
    return {k: getattr(s, k) for k in SettingsIn.model_fields}


@router.put("/settings")
async def put_settings(body: SettingsIn, ctx: OwnerDep) -> dict[str, Any]:
    s = await books.setup(ctx.session, ctx.business)
    for k, v in body.model_dump(exclude_unset=True).items():
        value = sorted({int(d) for d in v if 1 <= int(d) <= 120}) if k == "dunning_days" and v is not None else v
        setattr(s, k, value)
    return {k: getattr(s, k) for k in SettingsIn.model_fields}


@router.get("/invoices")
async def list_invoices(ctx: BusinessDep, status: str | None = None) -> dict[str, Any]:
    stmt = select(Invoice).order_by(Invoice.created_at.desc()).limit(200)
    if status:
        stmt = stmt.where(Invoice.status.in_(status.split(",")))
    return {"items": [fin.invoice_view(i) for i in (await ctx.session.execute(stmt)).scalars()]}


@router.post("/invoices", status_code=201)
async def create_invoice(body: InvoiceIn, ctx: EditorDep) -> dict[str, Any]:
    inv, _ = await fin.create_invoice(
        ctx.session,
        ctx.business,
        source_key=f"manual:{uuid.uuid4()}",
        lines=[line.model_dump() for line in body.lines],
        currency=body.currency,
        contact_id=body.contact_id,
        notes=body.notes,
        actor=Actor.user(ctx.user.id),
    )
    return fin.invoice_view(inv)


async def _invoice(ctx: BusinessDep, invoice_id: uuid.UUID) -> Invoice:
    inv = await ctx.session.get(Invoice, invoice_id, with_for_update=True)
    if inv is None:
        raise NotFound("Invoice not found")
    return inv


@router.post("/invoices/{invoice_id}/send")
async def send_invoice(invoice_id: uuid.UUID, ctx: EditorDep) -> dict[str, Any]:
    inv = await _invoice(ctx, invoice_id)
    if inv.status != "draft":
        raise ValidationFailed("Only drafts can be sent")
    decision_id = await fin.propose_send(ctx.session, ctx.business, inv, why="You asked to send it.")
    return fin.invoice_view(inv) | {"decision_id": str(decision_id) if decision_id else None}


@router.post("/invoices/{invoice_id}/void")
async def void_invoice(invoice_id: uuid.UUID, ctx: OwnerDep) -> dict[str, Any]:
    inv = await _invoice(ctx, invoice_id)
    await fin.void(ctx.session, ctx.business, inv, actor=Actor.user(ctx.user.id))
    return fin.invoice_view(inv)


@router.get("/bills")
async def list_bills(ctx: BusinessDep, status: str | None = None) -> dict[str, Any]:
    stmt = select(Bill).order_by(Bill.due_date).limit(200)
    if status:
        stmt = stmt.where(Bill.status.in_(status.split(",")))
    return {"items": [fin.bill_view(b) for b in (await ctx.session.execute(stmt)).scalars()]}


@router.post("/bills", status_code=201)
async def add_bill(body: BillIn, ctx: EditorDep) -> dict[str, Any]:
    bill, _ = await fin.book_bill(
        ctx.session,
        ctx.business,
        source_key=f"manual:{uuid.uuid4()}",
        supplier=body.supplier,
        category=body.category,
        amount_minor=body.amount_minor,
        currency=body.currency or ctx.business.currency,
        due_date=body.due_date,
        notes=body.notes,
    )
    await fin.propose_payment(ctx.session, ctx.business, bill, why="A bill was added.")
    return fin.bill_view(bill)


@router.get("/tax")
async def tax_filings(ctx: BusinessDep) -> dict[str, Any]:
    rows = (await ctx.session.execute(select(TaxFiling).order_by(TaxFiling.period_start.desc()).limit(48))).scalars()
    return {"items": [fin.filing_view(f) for f in rows]}


@router.get("/reports/pnl")
async def pnl(ctx: BusinessDep, start: date | None = None, end: date | None = None) -> dict[str, Any]:
    await books.setup(ctx.session, ctx.business)
    today = today_in(ctx.business.timezone)
    return await books.profit_and_loss(ctx.session, start or today.replace(day=1), end or today + timedelta(days=1))


@router.get("/reports/balance-sheet")
async def balance_sheet(ctx: BusinessDep) -> dict[str, Any]:
    await books.setup(ctx.session, ctx.business)
    return await books.balance_sheet(ctx.session)


@router.get("/reports/runway")
async def runway(ctx: BusinessDep) -> dict[str, Any]:
    await books.setup(ctx.session, ctx.business)
    return await books.runway(ctx.session, today_in(ctx.business.timezone))


class PlanIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    amount_minor: int = Field(gt=0)
    interval: str = Field(default="month", pattern="^(month|year)$")
    currency: str | None = Field(default=None, min_length=3, max_length=3)


def _plan_view(ctx: BusinessDep, p: Plan) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "name": p.name,
        "amount_minor": p.amount_minor,
        "currency": p.currency,
        "interval": p.interval,
        "active": p.active,
        "subscribe_url": subscribe_url(ctx.services, ctx.business_id, p.id),
    }


@router.get("/plans")
async def plans(ctx: BusinessDep) -> dict[str, Any]:
    rows = (await ctx.session.execute(select(Plan).order_by(Plan.amount_minor))).scalars()
    return {"items": [_plan_view(ctx, p) for p in rows]}


@router.post("/plans", status_code=201)
async def add_plan(body: PlanIn, ctx: OwnerDep) -> dict[str, Any]:
    p = Plan(
        business_id=ctx.business_id,
        name=body.name,
        amount_minor=body.amount_minor,
        interval=body.interval,
        currency=(body.currency or ctx.business.currency).upper(),
    )
    ctx.session.add(p)
    await ctx.session.flush()
    return _plan_view(ctx, p)


@router.post("/plans/{plan_id}/retire")
async def retire_plan(plan_id: uuid.UUID, ctx: OwnerDep) -> dict[str, Any]:
    p = await ctx.session.get(Plan, plan_id)
    if p is None:
        raise NotFound("Plan not found")
    p.active = False  # existing subscribers keep it; nobody new can subscribe
    return _plan_view(ctx, p)


@router.get("/subscriptions")
async def subscriptions(ctx: BusinessDep) -> dict[str, Any]:
    rows = (
        await ctx.session.execute(
            select(CustomerSubscription).order_by(CustomerSubscription.created_at.desc()).limit(500)
        )
    ).scalars()
    return {
        "items": [
            {
                "id": str(s.id),
                "plan_id": str(s.plan_id) if s.plan_id else None,
                "contact_id": str(s.contact_id) if s.contact_id else None,
                "status": s.status,
                "current_period_end": s.current_period_end.isoformat() if s.current_period_end else None,
            }
            for s in rows
        ]
    }


@router.post("/subscriptions/{subscription_id}/cancel")
async def cancel(subscription_id: uuid.UUID, ctx: OwnerDep) -> dict[str, Any]:
    sub = await ctx.session.get(CustomerSubscription, subscription_id, with_for_update=True)
    if sub is None:
        raise NotFound("Subscription not found")
    await cancel_subscription(ctx.services, ctx.business_id, sub)
    return {"id": str(sub.id), "status": sub.status}


@router.get("/invoices/{invoice_id}/pay-link")
async def invoice_pay_link(invoice_id: uuid.UUID, ctx: BusinessDep) -> dict[str, str]:
    inv = await ctx.session.get(Invoice, invoice_id)
    if inv is None:
        raise NotFound("Invoice not found")
    return {"url": pay_url(ctx.services, ctx.business_id, inv.id)}
