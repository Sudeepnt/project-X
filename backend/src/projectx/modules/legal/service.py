"""Legal and admin at work: paper every relationship, keep every deadline, open and close every door.

From other departments' events:
- deal won (GTM) → a customer agreement drafted from the template with the deal's facts; a person sends it for
  signature (signing is a one-way door), and once signed its dates become reminders,
- someone hired (People) → employment contract and NDA drafted; a set-up task for every standard account,
- someone leaving or left (People) → a removal task for every access they hold; things they owned are flagged,
- invoice escalated (Finance) → a formal payment demand drafted; sending it waits for a person.

Every day: renewals of software and domains are announced to Finance ahead of time (it books the bill),
contracts roll over or expire, and each quarter a person reviews who still has access.
Uploaded contracts: an open model lists the obligations; code keeps only those whose quoted words are really
in the contract and whose date is real, and puts each on the reminders.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, time, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.money import format_money
from projectx.kernel.time import today_in, utcnow, zone
from projectx.modules.legal.models import Access, Asset, Contract, LegalSettings, Obligation
from projectx.services import Services
from projectx.spine.bus import BusContext, on
from projectx.spine.decisions import service as decisions
from projectx.spine.decisions.executors import ExecutionContext, ExecutionResult, executor
from projectx.spine.gateway.contracts import ChatMessage, GatewayError, ModelRequest
from projectx.spine.gateway.offline import offline_generator
from projectx.spine.jobs.queue import enqueue
from projectx.spine.jobs.registry import JobContext, job
from projectx.spine.ledger.events import EventData, register_event
from projectx.spine.ledger.service import Actor, append
from projectx.spine.lifecycle import queue_email
from projectx.spine.notifications.service import notify
from projectx.spine.policy.actions import ActionType, register_action
from projectx.spine.policy.levels import Level
from projectx.spine.tenancy.models import Business
from projectx.spine.twin.models import Contact, Deal

AGENT = Actor.agent("legal")


def _task(key: str, module: str, title: str, description: str, *, reversible: bool) -> ActionType:
    return register_action(
        ActionType(
            key=key,
            module=module,
            pack=f"{module}.tasks",
            title=title,
            description=description,
            default_level=Level.DRAFT,
            max_level=Level.DRAFT,
            reversible=reversible,
        )
    )


SIGN = _task(
    "legal.sign_contract", "legal", "Get a contract signed", "A person sends it and marks it signed.", reversible=False
)
GRANT = _task(
    "admin.grant_access",
    "admin",
    "Set up an account",
    "A person creates the account or hands over the device.",
    reversible=True,
)
REVOKE = _task(
    "admin.revoke_access",
    "admin",
    "Remove access",
    "A person removes the account or collects the device.",
    reversible=True,
)
REVIEW = _task(
    "admin.access_review",
    "admin",
    "Review who has access",
    "A person checks every account is still needed.",
    reversible=True,
)
DEMAND = register_action(
    ActionType(
        key="legal.send_demand_letter",
        module="legal",
        pack="legal.collections",
        title="Send a formal payment demand",
        description="Email a firm, polite demand for a long-overdue invoice.",
        default_level=Level.ASK_FIRST,
        max_level=Level.ASK_FIRST,
        reversible=False,
    )
)


class ContractChanged(EventData):
    contract_id: str
    kind: str
    status: str
    value_minor: int = 0
    end_date: str | None = None


class AccessChanged(EventData):
    access_id: str
    employee_id: str
    status: str


class AssetRenewing(EventData):
    asset_id: str
    kind: str
    label: str
    vendor: str
    cost_minor: int
    currency: str
    renews_on: str


for _k, _d, _s in (
    ("legal.contract.drafted", "A contract was drafted", ContractChanged),
    ("legal.contract.signed", "A contract was signed", ContractChanged),
    ("legal.contract.renewed", "A contract rolled over", ContractChanged),
    ("legal.contract.expired", "A contract ended", ContractChanged),
    ("admin.access.granted", "An account was set up", AccessChanged),
    ("admin.access.revoked", "An account was removed", AccessChanged),
    ("admin.asset.renewing", "Software, a domain or a certificate renews soon", AssetRenewing),
):
    register_event(_k, _d, _s)

TEMPLATES = {
    "customer": (
        'SERVICE AGREEMENT\n\nBetween {business} ("we") and {counterparty} ("you").\n\n'
        "1. Work. We will deliver: {title}.\n2. Price. {value}, plus applicable tax, invoiced on {billing}.\n"
        "3. Payment. Within {terms} days of each invoice.\n4. Start. {start}.\n"
        "5. Changes. Agreed in writing by both of us.\n"
        "6. Ending. Either of us may end this with {notice} days' notice.\n"
    ),
    "employment": (
        "EMPLOYMENT AGREEMENT\n\nBetween {business} and {counterparty}.\n\nRole: {title}. Start date: {start}.\n"
        "Pay: {value} a month, paid monthly. Notice: {notice} days on either side after probation.\n"
    ),
    "nda": (
        "MUTUAL NON-DISCLOSURE AGREEMENT\n\nBetween {business} and {counterparty}, from {start}.\n"
        "Each of us keeps the other's confidential information private and uses it only for working together.\n"
    ),
    "demand": (
        "Dear {counterparty},\n\nInvoice {number} for {value} was due on {due}. "
        "Despite our reminders it remains unpaid.\n"
        "Please pay within 7 days or contact us to agree a plan. "
        "If we don't hear from you we may take further steps.\n\n"
        "{business}"
    ),
}


class _Safe(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


async def settings_for(session: AsyncSession, business_id: uuid.UUID) -> LegalSettings:
    s = await session.get(LegalSettings, business_id)
    if s is None:
        s = LegalSettings(business_id=business_id)
        session.add(s)
        await session.flush()
    return s


def render(kind: str, settings: LegalSettings, **facts: Any) -> str:
    template = (settings.templates or {}).get(kind) or TEMPLATES[kind]
    return str(template).format_map(_Safe(facts))


def _c(c: Contract) -> dict[str, Any]:
    return {
        "contract_id": str(c.id),
        "kind": c.kind,
        "status": c.status,
        "value_minor": c.value_minor,
        "end_date": c.end_date.isoformat() if c.end_date else None,
    }


async def draft_contract(
    session: AsyncSession,
    business: Business,
    *,
    kind: str,
    title: str,
    counterparty: str,
    source_key: str,
    body: str,
    value_minor: int = 0,
    start_date: date | None = None,
    end_date: date | None = None,
    contact_id: uuid.UUID | None = None,
    employee_id: uuid.UUID | None = None,
    deal_id: uuid.UUID | None = None,
    cause: dict[str, Any] | None = None,
) -> tuple[Contract, bool]:
    existing = await session.scalar(select(Contract).where(Contract.source_key == source_key))
    if existing is not None:
        return existing, False
    c = Contract(
        business_id=business.id,
        kind=kind,
        title=title[:200],
        counterparty=counterparty[:200],
        source_key=source_key[:120],
        body=body,
        value_minor=value_minor,
        currency=business.currency,
        start_date=start_date,
        end_date=end_date,
        contact_id=contact_id,
        employee_id=employee_id,
        deal_id=deal_id,
    )
    session.add(c)
    await session.flush()
    await append(
        session,
        business_id=business.id,
        type="legal.contract.drafted",
        actor=AGENT,
        subject=("contract", c.id),
        data=_c(c),
        **(cause or {}),
    )
    outcome = await decisions.propose(
        session,
        business.id,
        decisions.Proposal(
            action_type=SIGN.key,
            title=f"Get “{c.title}” signed with {c.counterparty}",
            summary="Drafted from your template. Review it, send it for signature, then mark this done.",
            payload={"contract_id": str(c.id)},
            preview={"body": body[:5000]},
            subject=("contract", c.id),
            requested_by="pack:legal.contracts",
            priority=35,
        ),
    )
    c.decision_id = outcome.decision_id
    return c, True


@on("gtm.deal.changed", "legal.draft_customer_agreement", when=lambda d: d.get("status") == "won")
async def on_deal_won(ctx: BusContext) -> dict[str, Any] | None:
    async with ctx.services.db.tenant(ctx.business_id) as session:
        deal = await session.get(Deal, uuid.UUID(ctx.data["deal_id"]))
        business = await session.get(Business, ctx.business_id)
        if deal is None or business is None:
            return {"skipped": "deal gone"}
        settings = await settings_for(session, business.id)
        if deal.value_minor < settings.contract_min_value_minor:
            return {"skipped": "below the contract threshold"}
        contact = await session.get(Contact, deal.contact_id) if deal.contact_id else None
        who = (contact.company or contact.name) if contact else None
        start = today_in(business.timezone)
        body = render(
            "customer",
            settings,
            business=business.name,
            counterparty=who or "the customer",
            title=deal.title,
            value=format_money(deal.value_minor, deal.currency),
            billing="milestones" if (deal.attributes or {}).get("billing") == "milestones" else "signing",
            terms=14,
            start=start.isoformat(),
            notice=30,
        )
        c, created = await draft_contract(
            session,
            business,
            kind="customer",
            title=f"Service agreement: {deal.title}",
            counterparty=who or "Customer",
            source_key=f"deal:{deal.id}",
            body=body,
            value_minor=deal.value_minor,
            start_date=start,
            contact_id=deal.contact_id,
            deal_id=deal.id,
            cause=ctx.cause,
        )
        return {"contract_id": str(c.id), "created": created}


@on("people.employee.hired", "legal.onboard_employee")
async def on_hired(ctx: BusContext) -> dict[str, Any] | None:
    from projectx.modules.people.models import Employee

    async with ctx.services.db.tenant(ctx.business_id) as session:
        e = await session.get(Employee, uuid.UUID(ctx.data["employee_id"]))
        business = await session.get(Business, ctx.business_id)
        if e is None or business is None:
            return {"skipped": "employee gone"}
        settings = await settings_for(session, business.id)
        for kind, title in (("employment", f"Employment agreement: {e.name}"), ("nda", f"NDA: {e.name}")):
            await draft_contract(
                session,
                business,
                kind=kind,
                title=title,
                counterparty=e.name,
                source_key=f"{kind}:{e.id}",
                body=render(
                    kind,
                    settings,
                    business=business.name,
                    counterparty=e.name,
                    title=e.role,
                    start=e.start_date.isoformat(),
                    value=format_money(e.monthly_salary_minor, e.currency),
                    notice=30,
                ),
                start_date=e.start_date,
                employee_id=e.id,
                cause=ctx.cause,
            )
        tasks = 0
        for system in settings.standard_access:
            if await session.scalar(select(Access.id).where(Access.employee_id == e.id, Access.system == system)):
                continue
            access = Access(business_id=business.id, employee_id=e.id, system=str(system)[:80])
            session.add(access)
            await session.flush()
            outcome = await decisions.propose(
                session,
                business.id,
                decisions.Proposal(
                    action_type=GRANT.key,
                    title=f"Set up {system} for {e.name} before {e.start_date:%d %b}",
                    payload={"access_id": str(access.id)},
                    subject=("access", access.id),
                    requested_by="pack:admin.tasks",
                    priority=40,
                    expires_at=None,
                ),
            )
            access.decision_id = outcome.decision_id
            tasks += 1
        return {"contracts": 2, "access_tasks": tasks}


@on("people.employee.leaving", "admin.offboard_employee")
async def on_leaving(ctx: BusContext) -> dict[str, Any] | None:
    from projectx.modules.people.models import Employee

    async with ctx.services.db.tenant(ctx.business_id) as session:
        e = await session.get(Employee, uuid.UUID(ctx.data["employee_id"]))
        if e is None:
            return {"skipped": "employee gone"}
        last = e.end_date.isoformat() if e.end_date else "their last day"
        doors = list(
            (
                await session.execute(
                    select(Access).where(Access.employee_id == e.id, Access.status.in_(("granted", "to_grant")))
                )
            ).scalars()
        )
        for access in doors:
            access.status = "to_revoke"
            outcome = await decisions.propose(
                session,
                ctx.business_id,
                decisions.Proposal(
                    action_type=REVOKE.key,
                    title=f"Remove {access.system} for {e.name} on {last}",
                    payload={"access_id": str(access.id)},
                    subject=("access", access.id),
                    requested_by="pack:admin.tasks",
                    priority=15,
                ),
            )
            access.decision_id = outcome.decision_id
        owned = list(
            (
                await session.execute(select(Asset).where(Asset.owner_employee_id == e.id, Asset.status == "active"))
            ).scalars()
        )
        if owned:
            await notify(
                session,
                ctx.business_id,
                kind="admin.reassign",
                level="warning",
                title=f"{e.name} looks after {len(owned)} subscription(s): pick a new owner",
                body=", ".join(a.name for a in owned[:10]),
                link="/admin/assets",
            )
        return {"revoke_tasks": len(doors), "assets_to_reassign": len(owned)}


@on("finance.invoice.escalated", "legal.draft_demand", when=lambda d: bool(d.get("contact_id")))
async def on_escalated(ctx: BusContext) -> dict[str, Any] | None:
    from projectx.modules.finance.models import Invoice

    async with ctx.services.db.tenant(ctx.business_id) as session:
        inv = await session.get(Invoice, uuid.UUID(ctx.data["invoice_id"]))
        business = await session.get(Business, ctx.business_id)
        contact = await session.get(Contact, uuid.UUID(ctx.data["contact_id"]))
        if inv is None or business is None or contact is None or contact.erased_at:
            return {"skipped": "nothing to send"}
        settings = await settings_for(session, business.id)
        owed = format_money(inv.total_minor - inv.paid_minor, inv.currency)
        text = render(
            "demand",
            settings,
            counterparty=contact.name or contact.company or "customer",
            number=inv.number,
            value=owed,
            due=inv.due_date.isoformat() if inv.due_date else "",
            business=business.name,
        )
        await decisions.propose(
            session,
            business.id,
            decisions.Proposal(
                action_type=DEMAND.key,
                title=f"Send a formal demand for {inv.number} ({owed})",
                summary=text[:500],
                payload={"invoice_id": str(inv.id), "contact_id": str(contact.id), "text": text},
                preview={"letter": text},
                why_now="It is long overdue and the reminders didn't work.",
                if_no="Finance keeps it open; you may prefer to call them first.",
                subject=("invoice", inv.id),
                requested_by="pack:legal.collections",
                priority=20,
            ),
        )
        return {"drafted": inv.number}


@executor(DEMAND.key)
async def _send_demand(ctx: ExecutionContext) -> ExecutionResult:
    async with ctx.services.db.tenant(ctx.business_id) as session:
        contact = await session.get(Contact, uuid.UUID(ctx.payload["contact_id"]))
        if contact is None or not contact.email or contact.erased_at:
            return ExecutionResult(data={"skipped": "no email"})
        text = str(ctx.payload["text"])
        await queue_email(
            session,
            to=contact.email,
            subject="Overdue invoice: payment required",
            html="<p>" + text.replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br>") + "</p>",
            text=text,
            business_id=ctx.business_id,
        )
        return ExecutionResult(data={"sent": True}, summary="Sent the payment demand.")


# ---------------------------------------------------------------- a person did the task


async def on_task_done(services: Services, business_id: uuid.UUID, decision: Any) -> None:
    payload = decision.payload or {}
    async with services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        if business is None:
            return
        if decision.action_type == SIGN.key:
            c = await session.get(Contract, uuid.UUID(payload["contract_id"]), with_for_update=True)
            if c is None or c.status != "draft":
                return
            c.status, c.signed_at = "active", utcnow()
            await append(
                session,
                business_id=business_id,
                type="legal.contract.signed",
                actor=AGENT,
                subject=("contract", c.id),
                data=_c(c),
            )
            await _contract_reminders(session, business, c)
        elif decision.action_type in (GRANT.key, REVOKE.key):
            access = await session.get(Access, uuid.UUID(payload["access_id"]), with_for_update=True)
            if access is None:
                return
            if decision.action_type == GRANT.key:
                access.status, access.granted_at, type_ = "granted", utcnow(), "admin.access.granted"
            else:
                access.status, access.revoked_at, type_ = "revoked", utcnow(), "admin.access.revoked"
            await append(
                session,
                business_id=business_id,
                type=type_,
                actor=AGENT,
                subject=("access", access.id),
                data={"access_id": str(access.id), "employee_id": str(access.employee_id), "status": access.status},
            )


async def _contract_reminders(session: AsyncSession, business: Business, c: Contract) -> None:
    from projectx.spine.calendar.service import add_reminder

    if c.end_date is None:
        return
    deadline = c.end_date - timedelta(days=c.notice_days)
    for title, when, key in (
        (f"Last day to give notice on “{c.title}”", deadline, "notice"),
        (f"“{c.title}” ends" + (" and renews" if c.auto_renews else ""), c.end_date, "end"),
    ):
        at = datetime.combine(when, time(9), tzinfo=zone(business.timezone))
        if at > utcnow():
            await add_reminder(
                session,
                business,
                title=title[:200],
                due_at=at,
                kind="renewal" if key == "end" else "deadline",
                importance="high",
                all_day=True,
                notify_before_minutes=[14 * 1440, 3 * 1440],
                source="agent",
                dedupe_key=f"contract:{c.id}:{key}:{when}",
                actor=AGENT,
            )


# ---------------------------------------------------------------- the daily round


def next_renewal(day: date, billing: str) -> date:
    if billing == "monthly":
        y, m = divmod(day.month, 12)
        nxt = date(day.year + y, m + 1, 1)
        import calendar

        return nxt.replace(day=min(day.day, calendar.monthrange(nxt.year, nxt.month)[1]))
    try:
        return day.replace(year=day.year + 1)
    except ValueError:  # 29 February
        return day.replace(year=day.year + 1, day=28)


async def daily(services: Services, business_id: uuid.UUID) -> None:
    async with services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        if business is None:
            return
        settings = await settings_for(session, business_id)
        today = today_in(business.timezone)
        assets = (
            await session.execute(
                select(Asset).where(Asset.status == "active", Asset.renews_on.is_not(None)).with_for_update()
            )
        ).scalars()
        for a in assets:
            assert a.renews_on is not None
            while a.renews_on < today and a.billing != "once":
                a.renews_on = next_renewal(a.renews_on, a.billing)
            if a.renews_on - today <= timedelta(days=settings.renewal_notice_days) and a.announced_for != a.renews_on:
                a.announced_for = a.renews_on
                await append(
                    session,
                    business_id=business_id,
                    type="admin.asset.renewing",
                    actor=AGENT,
                    subject=("asset", a.id),
                    data={
                        "asset_id": str(a.id),
                        "kind": a.kind,
                        "label": a.name,
                        "vendor": a.vendor,
                        "cost_minor": a.cost_minor,
                        "currency": a.currency,
                        "renews_on": a.renews_on.isoformat(),
                    },
                )
        for c in (
            await session.execute(
                select(Contract).where(Contract.status == "active", Contract.end_date < today).with_for_update()
            )
        ).scalars():
            assert c.end_date is not None
            if c.auto_renews and c.start_date:
                length = c.end_date - c.start_date
                c.start_date, c.end_date = c.end_date + timedelta(days=1), c.end_date + length + timedelta(days=1)
                await append(
                    session,
                    business_id=business_id,
                    type="legal.contract.renewed",
                    actor=AGENT,
                    subject=("contract", c.id),
                    data=_c(c),
                )
                await _contract_reminders(session, business, c)
            else:
                c.status = "expired"
                await append(
                    session,
                    business_id=business_id,
                    type="legal.contract.expired",
                    actor=AGENT,
                    subject=("contract", c.id),
                    data=_c(c),
                )
        if today.day == 1 and today.month in (1, 4, 7, 10):
            granted = await session.scalar(select(func.count()).select_from(Access).where(Access.status == "granted"))
            if granted:
                await decisions.propose(
                    session,
                    business_id,
                    decisions.Proposal(
                        action_type=REVIEW.key,
                        title=f"Quarterly check: are all {granted} accounts still needed?",
                        payload={"quarter": f"{today.year}-Q{(today.month - 1) // 3 + 1}"},
                        requested_by="pack:admin.tasks",
                        priority=50,
                    ),
                )


# ---------------------------------------------------------------- obligations from an uploaded contract


class Found(BaseModel):
    kind: Literal["payment", "delivery", "notice", "renewal", "other"]
    text: str = Field(description="What must be done, one plain sentence")
    due: str = Field(description="The date it is due, YYYY-MM-DD, worked out from the contract")
    evidence: str = Field(description="The exact words of the contract that create it")


class Obligations(BaseModel):
    items: list[Found]


def _n(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


@job("legal.extract_obligations", max_attempts=4, timeout_s=180)
async def extract(ctx: JobContext) -> dict[str, Any]:
    business_id = ctx.require_business()
    async with ctx.services.db.tenant(business_id) as session:
        c = await session.get(Contract, uuid.UUID(ctx.payload["contract_id"]))
        if c is None:
            return {"skipped": "gone"}
        body, start = c.body[:30_000], c.start_date
    try:
        found = (
            await ctx.services.gateway.run(
                business_id,
                ModelRequest(
                    task="legal.obligations",
                    system="You read a contract and list each dated obligation: payments, deliveries, notice periods, "
                    f"renewals. The contract starts on {start or 'its signing date'}. Quote the exact words.",
                    messages=[ChatMessage("user", body)],
                    output_schema=Obligations,
                    max_tokens=2000,
                    rung="mid",
                    context={"text": body},
                ),
            )
        ).parsed_as(Obligations)
    except GatewayError:
        return {"skipped": "model unavailable"}
    kept = 0
    async with ctx.services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        assert business is not None
        from projectx.spine.calendar.service import add_reminder

        for item in found.items[:30]:
            if len(_n(item.evidence)) < 8 or _n(item.evidence) not in _n(body):
                continue
            try:
                due = date.fromisoformat(item.due.strip())
            except ValueError:
                continue
            if due < today_in(business.timezone):
                continue
            ob = Obligation(
                business_id=business_id,
                contract_id=uuid.UUID(ctx.payload["contract_id"]),
                kind=item.kind,
                text=item.text[:500],
                evidence=item.evidence[:2000],
                due_date=due,
            )
            session.add(ob)
            await session.flush()
            reminder = await add_reminder(
                session,
                business,
                title=item.text[:200],
                due_at=datetime.combine(due, time(9), tzinfo=zone(business.timezone)),
                kind="payment" if item.kind == "payment" else "deadline",
                notes=f"From the contract: “{item.evidence[:300]}”",
                all_day=True,
                source="agent",
                dedupe_key=f"obligation:{ob.id}",
                actor=AGENT,
            )
            ob.reminder_id = reminder.id
            kept += 1
    return {"found": len(found.items), "kept": kept}


async def upload_contract(session: AsyncSession, business: Business, **fields: Any) -> Contract:
    c = Contract(
        business_id=business.id,
        currency=business.currency,
        source_key=f"upload:{uuid.uuid4()}",
        status="active",
        **fields,
    )
    session.add(c)
    await session.flush()
    await append(
        session,
        business_id=business.id,
        type="legal.contract.signed",
        actor=AGENT,
        subject=("contract", c.id),
        data=_c(c),
    )
    await _contract_reminders(session, business, c)
    await enqueue(
        session,
        "legal.extract_obligations",
        {"contract_id": str(c.id)},
        business_id=business.id,
        idempotency_key=f"oblig:{c.id}",
    )
    return c


_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


@offline_generator("legal.obligations")
def _offline_obligations(req: ModelRequest) -> dict[str, Any]:
    text = str(req.context.get("text", ""))
    items = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        m = _DATE.search(sentence)
        if m:
            lower = sentence.lower()
            kind = (
                "payment"
                if "pay" in lower
                else "notice"
                if "notice" in lower
                else "delivery"
                if "deliver" in lower
                else "other"
            )
            items.append(
                {"kind": kind, "text": sentence.strip()[:200], "due": m.group(1), "evidence": sentence.strip()}
            )
    return {"items": items}


# ---------------------------------------------------------------- views and numbers


def contract_view(c: Contract) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "kind": c.kind,
        "title": c.title,
        "counterparty": c.counterparty,
        "status": c.status,
        "value_minor": c.value_minor,
        "currency": c.currency,
        "start_date": c.start_date.isoformat() if c.start_date else None,
        "end_date": c.end_date.isoformat() if c.end_date else None,
        "auto_renews": c.auto_renews,
        "notice_days": c.notice_days,
        "decision_id": str(c.decision_id) if c.decision_id else None,
        "body": c.body,
    }


def asset_view(a: Asset) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "kind": a.kind,
        "name": a.name,
        "vendor": a.vendor,
        "cost_minor": a.cost_minor,
        "currency": a.currency,
        "billing": a.billing,
        "renews_on": a.renews_on.isoformat() if a.renews_on else None,
        "seats": a.seats,
        "status": a.status,
        "owner_employee_id": str(a.owner_employee_id) if a.owner_employee_id else None,
    }


async def numbers(session: AsyncSession, business_id: uuid.UUID) -> dict[str, Any]:
    soon = date.today() + timedelta(days=60)
    active = await session.scalar(select(func.count()).select_from(Contract).where(Contract.status == "active"))
    ending = await session.scalar(
        select(func.count()).select_from(Contract).where(Contract.status == "active", Contract.end_date <= soon)
    )
    unsigned = await session.scalar(select(func.count()).select_from(Contract).where(Contract.status == "draft"))
    monthly = await session.scalar(
        select(
            func.coalesce(
                func.sum(
                    case(
                        (Asset.billing == "monthly", Asset.cost_minor),
                        (Asset.billing == "yearly", Asset.cost_minor / 12),
                        else_=0,
                    )
                ),
                0,
            )
        ).where(Asset.status == "active")
    )
    open_doors = await session.scalar(
        select(func.count()).select_from(Access).where(Access.status.in_(("to_grant", "to_revoke")))
    )
    return {
        "active_contracts": int(active or 0),
        "contracts_ending_60d": int(ending or 0),
        "unsigned_contracts": int(unsigned or 0),
        "software_monthly_minor": int(monthly or 0),
        "open_access_tasks": int(open_doors or 0),
    }
