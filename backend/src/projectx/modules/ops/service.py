"""Operations at work: deliver what was sold, and look after customers.

By itself, from other departments' events:
- deal won (GTM) → a delivery project with tasks and dates; a kickoff call is booked in the first free slot
  (the invitation waits on the usual decision card),
- invoice escalated (Finance) → the customer's active projects go on hold, with the reason,
- feedback linked or released (Product) → the ticket remembers the item; when it ships, the customer is told.

Support: a new ticket is triaged by an open model (kind, priority, sentiment), answered only from published
help articles (a draft reply waits for approval until replies earn the right to go alone), feature requests
and bugs are handed to Product, and first-response deadlines are watched. Completing a billable milestone
hands it to Finance to invoice.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.errors import NotFound
from projectx.kernel.time import today_in, utcnow
from projectx.modules.ops.models import Article, Project, Task, Ticket
from projectx.services import Services
from projectx.spine.bus import BusContext, on
from projectx.spine.decisions import service as decisions
from projectx.spine.decisions.executors import ExecutionContext, ExecutionResult, executor
from projectx.spine.gateway.contracts import ChatMessage, GatewayError, ModelRequest
from projectx.spine.gateway.offline import offline_generator
from projectx.spine.jobs.queue import enqueue
from projectx.spine.jobs.registry import JobContext, job, periodic
from projectx.spine.ledger.events import EventData, register_event
from projectx.spine.ledger.service import Actor, append
from projectx.spine.lifecycle import queue_email
from projectx.spine.notifications.service import notify
from projectx.spine.policy.actions import ActionType, register_action
from projectx.spine.policy.levels import Level
from projectx.spine.tenancy.models import Business
from projectx.spine.twin.models import Contact, Deal

M = "ops"
AGENT = Actor.agent("ops")
SLA_HOURS = {"urgent": 2, "high": 8, "normal": 24, "low": 72}
DEFAULT_PLAN: tuple[tuple[str, int, bool], ...] = (
    ("Kickoff call", 2, False),
    ("Deliver the work", 14, True),
    ("Review with the customer", 17, True),
    ("Close out and ask for feedback", 21, False),
)

REPLY = register_action(
    ActionType(
        key="ops.reply_ticket",
        module=M,
        pack="ops.support",
        title="Answer a support ticket",
        description="Email the customer an answer drawn from a published help article.",
        default_level=Level.ASK_FIRST,
        max_level=Level.RUN,
        reversible=True,
        undo="Emails can't be recalled: send a correction.",
    )
)


class TicketChanged(EventData):
    ticket_id: str
    status: str
    kind: str
    priority: str
    contact_id: str | None = None


class Feedback(EventData):
    ticket_id: str
    kind: str  # feature | bug
    contact_id: str | None = None


class ProjectChanged(EventData):
    project_id: str
    status: str
    deal_id: str | None = None
    contact_id: str | None = None


class MilestoneDone(EventData):
    task_id: str
    project_id: str
    label: str
    billable_minor: int
    currency: str
    contact_id: str | None = None


for _k, _d, _s in (
    ("ops.ticket.created", "A support ticket came in", TicketChanged),
    ("ops.ticket.triaged", "A ticket was sorted by kind and priority", TicketChanged),
    ("ops.ticket.solved", "A ticket was solved", TicketChanged),
    ("ops.feedback.received", "A customer asked for a feature or reported a bug", Feedback),
    ("ops.project.started", "A delivery project started", ProjectChanged),
    ("ops.project.on_hold", "A project was put on hold", ProjectChanged),
    ("ops.project.completed", "A delivery project finished", ProjectChanged),
    ("ops.milestone.completed", "A milestone was delivered", MilestoneDone),
):
    register_event(_k, _d, _s)


# ---------------------------------------------------------------- delivery


@on("gtm.deal.changed", "ops.start_project", when=lambda d: d.get("status") == "won")
async def on_deal_won(ctx: BusContext) -> dict[str, Any] | None:
    async with ctx.services.db.tenant(ctx.business_id) as session:
        deal = await session.get(Deal, uuid.UUID(ctx.data["deal_id"]))
        business = await session.get(Business, ctx.business_id)
        if deal is None or business is None:
            return {"skipped": "deal gone"}
        if await session.scalar(select(Project.id).where(Project.deal_id == deal.id)):
            return {"skipped": "already started"}
        project = await start_project(session, business, deal, cause=ctx.cause)
        return {"project_id": str(project.id)}


async def start_project(session: AsyncSession, business: Business, deal: Deal, *, cause: dict[str, Any]) -> Project:
    today = today_in(business.timezone)
    attrs = deal.attributes or {}
    billing = "milestones" if attrs.get("billing") == "milestones" else "full"
    plan = [(t["title"], int(t.get("days", 7)), bool(t.get("milestone"))) for t in attrs.get("tasks", [])] or list(
        DEFAULT_PLAN
    )
    project = Project(
        business_id=business.id,
        deal_id=deal.id,
        contact_id=deal.contact_id,
        name=deal.title[:200],
        billing=billing,
        currency=deal.currency,
        started_on=today,
        due_on=today + timedelta(days=max(d for _, d, _ in plan)),
    )
    session.add(project)
    await session.flush()
    milestones = [p for p in plan if p[2]]
    share = deal.value_minor // len(milestones) if billing == "milestones" and milestones else 0
    remainder = deal.value_minor - share * len(milestones) if share else 0
    for i, (title, days, milestone) in enumerate(plan):
        billable = 0
        if milestone and share:
            billable = share + (remainder if (title, days, milestone) == milestones[-1] else 0)
        session.add(
            Task(
                business_id=business.id,
                project_id=project.id,
                title=title[:200],
                position=i,
                due_on=today + timedelta(days=days),
                milestone=milestone,
                billable_minor=billable,
            )
        )
    await session.flush()
    await _project_event(session, business.id, "ops.project.started", project, **cause)
    contact = await session.get(Contact, deal.contact_id) if deal.contact_id else None
    if contact and contact.email and not contact.erased_at:
        await _book_kickoff(session, business, project, contact)
    return project


async def _book_kickoff(session: AsyncSession, business: Business, project: Project, contact: Contact) -> None:
    """The first free 30-minute slot from tomorrow; the invitation goes on a decision card as usual."""
    from projectx.spine.calendar import service as cal

    start = utcnow() + timedelta(days=1)
    slots = await cal.find_free_time(
        session, business, start=start, end=start + timedelta(days=10), duration_minutes=30, limit=1
    )
    if not slots:
        return
    event, _ = await cal.schedule_event(
        session,
        business,
        title=f"Kickoff: {project.name}"[:200],
        starts_at=slots[0].start,
        ends_at=slots[0].end,
        attendees=[{"email": contact.email, "name": contact.name}],
        contact_id=contact.id,
        description="Plan the work together: goals, dates and who does what.",
        actor=AGENT,
    )
    project.kickoff_event_id = event.id


async def _project_event(session: AsyncSession, business_id: uuid.UUID, type_: str, p: Project, **cause: Any) -> None:
    await append(
        session,
        business_id=business_id,
        type=type_,
        actor=AGENT,
        subject=("project", p.id),
        data={
            "project_id": str(p.id),
            "status": p.status,
            "deal_id": str(p.deal_id) if p.deal_id else None,
            "contact_id": str(p.contact_id) if p.contact_id else None,
        },
        **cause,
    )


async def complete_task(session: AsyncSession, business: Business, task: Task, *, actor: Actor) -> Project:
    project = await session.get(Project, task.project_id, with_for_update=True)
    if project is None:
        raise NotFound("Project not found")
    if task.status == "done":
        return project
    task.status, task.done_at = "done", utcnow()
    if task.milestone:
        await append(
            session,
            business_id=business.id,
            type="ops.milestone.completed",
            actor=actor,
            subject=("task", task.id),
            data={
                "task_id": str(task.id),
                "project_id": str(project.id),
                "label": f"{project.name}: {task.title}"[:300],
                "billable_minor": task.billable_minor,
                "currency": project.currency,
                "contact_id": str(project.contact_id) if project.contact_id else None,
            },
        )
    remaining = await session.scalar(
        select(func.count())
        .select_from(Task)
        .where(Task.project_id == project.id, Task.status != "done", Task.id != task.id)
    )
    if not remaining and project.status != "done":
        project.status, project.done_at = "done", utcnow()
        await _project_event(session, business.id, "ops.project.completed", project)
    return project


@on("finance.invoice.escalated", "ops.hold_for_unpaid", when=lambda d: bool(d.get("contact_id")))
async def on_escalated(ctx: BusContext) -> dict[str, Any] | None:
    async with ctx.services.db.tenant(ctx.business_id) as session:
        held = []
        rows = (
            await session.execute(
                select(Project).where(
                    Project.contact_id == uuid.UUID(ctx.data["contact_id"]), Project.status == "active"
                )
            )
        ).scalars()
        for p in rows:
            p.status, p.hold_reason = "on_hold", "An invoice is long overdue; Legal is following up."
            await _project_event(session, ctx.business_id, "ops.project.on_hold", p, **ctx.cause)
            held.append(str(p.id))
        if held:
            await notify(
                session,
                ctx.business_id,
                kind="ops.on_hold",
                level="warning",
                title=f"{len(held)} project{'s' if len(held) != 1 else ''} on hold for an unpaid invoice",
                link="/operations",
            )
        return {"on_hold": held} if held else {"skipped": "no active projects"}


# ---------------------------------------------------------------- support


class Triage(BaseModel):
    kind: Literal["question", "problem", "feature", "bug", "billing"]
    priority: Literal["urgent", "high", "normal", "low"]
    sentiment: Literal["positive", "neutral", "negative"]
    summary: str = Field(description="One line, no personal details")


class Answer(BaseModel):
    can_answer: bool = Field(description="True only if the article fully answers the question")
    reply: str = Field(
        description="A short, kind reply that uses only facts from the article. Empty if can_answer is false"
    )
    quote: str = Field(description="The sentence of the article the answer rests on. Empty if can_answer is false")


async def open_ticket(
    session: AsyncSession,
    business: Business,
    *,
    subject: str,
    body: str,
    contact_id: uuid.UUID | None,
    channel: str = "email",
    actor: Actor,
) -> Ticket:
    ticket = Ticket(
        business_id=business.id,
        contact_id=contact_id,
        subject=subject[:300],
        body=body[:20_000],
        channel=channel,
        sla_due_at=utcnow() + timedelta(hours=SLA_HOURS["normal"]),
    )
    session.add(ticket)
    await session.flush()
    await append(
        session,
        business_id=business.id,
        type="ops.ticket.created",
        actor=actor,
        subject=("ticket", ticket.id),
        data=_t(ticket),
    )
    await enqueue(
        session,
        "ops.triage_ticket",
        {"ticket_id": str(ticket.id)},
        business_id=business.id,
        idempotency_key=f"triage:{ticket.id}",
    )
    return ticket


def _t(t: Ticket) -> dict[str, Any]:
    return {
        "ticket_id": str(t.id),
        "status": t.status,
        "kind": t.kind,
        "priority": t.priority,
        "contact_id": str(t.contact_id) if t.contact_id else None,
    }


@job("ops.triage_ticket", max_attempts=4, timeout_s=120)
async def triage(ctx: JobContext) -> dict[str, Any]:
    business_id = ctx.require_business()
    services = ctx.services
    async with services.db.tenant(business_id) as session:
        ticket = await session.get(Ticket, uuid.UUID(ctx.payload["ticket_id"]))
        if ticket is None:
            return {"skipped": "gone"}
        text = f"{ticket.subject}\n\n{ticket.body}"[:6000]
    try:
        sorted_ = (
            await services.gateway.run(
                business_id,
                ModelRequest(
                    task="ops.triage",
                    system="You sort customer support messages. Be calm about tone; urgent means the customer can't "
                    "work at all or money is at risk.",
                    messages=[ChatMessage("user", text)],
                    output_schema=Triage,
                    max_tokens=300,
                    rung="small",
                    max_rung="mid",
                    context={"text": text},
                ),
            )
        ).parsed_as(Triage)
    except GatewayError:
        sorted_ = Triage(kind="question", priority="normal", sentiment="neutral", summary=ticket.subject[:200])
    async with services.db.tenant(business_id) as session:
        ticket = await session.get(Ticket, uuid.UUID(ctx.payload["ticket_id"]), with_for_update=True)
        business = await session.get(Business, business_id)
        assert ticket is not None
        assert business is not None
        ticket.kind, ticket.priority, ticket.sentiment = sorted_.kind, sorted_.priority, sorted_.sentiment
        ticket.summary = sorted_.summary[:300]
        ticket.sla_due_at = ticket.created_at + timedelta(hours=SLA_HOURS[sorted_.priority])
        await append(
            session,
            business_id=business_id,
            type="ops.ticket.triaged",
            actor=AGENT,
            subject=("ticket", ticket.id),
            data=_t(ticket),
        )
        if ticket.kind in ("feature", "bug"):
            await append(
                session,
                business_id=business_id,
                type="ops.feedback.received",
                actor=AGENT,
                subject=("ticket", ticket.id),
                data={"ticket_id": str(ticket.id), "kind": ticket.kind, "contact_id": _t(ticket)["contact_id"]},
            )
        article = await best_article(session, text)
        article_view = (article.id, article.title, article.body[:6000]) if article else None
    if article_view is None:
        return {"kind": sorted_.kind, "answered": False}
    try:
        answer = (
            await services.gateway.run(
                business_id,
                ModelRequest(
                    task="ops.answer",
                    system="You answer a customer only from the help article given. If it doesn't fully answer the "
                    "question, say you can't. Never promise refunds, dates or anything not in the article.",
                    messages=[
                        ChatMessage("user", f"ARTICLE: {article_view[1]}\n{article_view[2]}\n\nCUSTOMER:\n{text}")
                    ],
                    output_schema=Answer,
                    max_tokens=600,
                    rung="small",
                    max_rung="mid",
                    context={"text": text, "article": article_view[2]},
                ),
            )
        ).parsed_as(Answer)
    except GatewayError:
        return {"kind": sorted_.kind, "answered": False}
    if not answer.can_answer or not answer.reply.strip() or not _quoted(answer.quote, article_view[2]):
        return {"kind": sorted_.kind, "answered": False}
    async with services.db.tenant(business_id) as session:
        ticket = await session.get(Ticket, uuid.UUID(ctx.payload["ticket_id"]), with_for_update=True)
        assert ticket is not None
        ticket.reply_draft, ticket.article_id = answer.reply.strip(), article_view[0]
        outcome = await decisions.propose(
            session,
            business_id,
            decisions.Proposal(
                action_type=REPLY.key,
                title=f"Answer “{ticket.subject[:120]}”",
                summary=answer.reply.strip()[:500],
                payload={"ticket_id": str(ticket.id), "text": answer.reply.strip()},
                preview={"reply": answer.reply.strip(), "article": article_view[1], "quote": answer.quote},
                why_now=f"Answers from the help article “{article_view[1]}”.",
                subject=("ticket", ticket.id),
                requested_by="pack:ops.support",
                priority=25 if ticket.priority in ("urgent", "high") else 45,
            ),
        )
        ticket.decision_id = outcome.decision_id
    return {"kind": sorted_.kind, "answered": True}


def _quoted(quote: str, article: str) -> bool:
    def n(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

    return len(n(quote)) >= 8 and n(quote) in n(article)


async def best_article(session: AsyncSession, text: str) -> Article | None:
    words = " OR ".join(re.findall(r"[A-Za-z]{4,}", text)[:20])
    if not words:
        return None
    tsq = func.websearch_to_tsquery("english", words)
    found: Article | None = await session.scalar(
        select(Article)
        .where(Article.published.is_(True), Article.search.op("@@")(tsq))
        .order_by(func.ts_rank(Article.search, tsq).desc())
        .limit(1)
    )
    return found


@executor(REPLY.key)
async def _reply(ctx: ExecutionContext) -> ExecutionResult:
    async with ctx.services.db.tenant(ctx.business_id) as session:
        ticket = await session.get(Ticket, uuid.UUID(ctx.payload["ticket_id"]), with_for_update=True)
        business = await session.get(Business, ctx.business_id)
        if ticket is None or business is None:
            raise NotFound("Ticket not found")
        contact = await session.get(Contact, ticket.contact_id) if ticket.contact_id else None
        text = str(ctx.payload["text"])
        if contact and contact.email and not contact.erased_at:
            await queue_email(
                session,
                to=contact.email,
                subject=f"Re: {ticket.subject}",
                html="<p>" + text.replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br>") + "</p>",
                text=text,
                business_id=business.id,
            )
        ticket.first_response_at = ticket.first_response_at or utcnow()
        ticket.status = "waiting"
        return ExecutionResult(data={"ticket_id": str(ticket.id)}, summary="Answered the customer.")


async def solve(session: AsyncSession, business_id: uuid.UUID, ticket: Ticket, *, actor: Actor) -> None:
    if ticket.status == "solved":
        return
    ticket.status, ticket.solved_at = "solved", utcnow()
    await append(
        session,
        business_id=business_id,
        type="ops.ticket.solved",
        actor=actor,
        subject=("ticket", ticket.id),
        data=_t(ticket),
    )


@periodic("ops.watch_sla", every=timedelta(minutes=15))
async def watch_sla(services: Services, now: Any) -> None:
    async with services.db.session() as session:
        business_ids = list((await session.execute(select(Business.id).where(Business.status == "active"))).scalars())
    for business_id in business_ids:
        async with services.db.tenant(business_id) as session:
            due = (
                await session.execute(
                    select(Ticket).where(
                        Ticket.status == "open",
                        Ticket.first_response_at.is_(None),
                        Ticket.sla_warned.is_(False),
                        Ticket.sla_due_at < now + timedelta(hours=1),
                    )
                )
            ).scalars()
            for t in due:
                t.sla_warned = True
                late = t.sla_due_at is not None and t.sla_due_at <= now
                await notify(
                    session,
                    business_id,
                    kind="ops.sla",
                    level="critical" if late else "warning",
                    title=f"{'Late' if late else 'Due within the hour'}: reply to “{t.subject[:120]}”",
                    body=t.summary,
                    link=f"/operations/tickets/{t.id}",
                )


@on("product.feedback.linked", "ops.remember_item")
async def on_linked(ctx: BusContext) -> dict[str, Any] | None:
    async with ctx.services.db.tenant(ctx.business_id) as session:
        ticket = await session.get(Ticket, uuid.UUID(ctx.data["ticket_id"]))
        if ticket is None:
            return {"skipped": "ticket gone"}
        ticket.product_item_id = uuid.UUID(ctx.data["item_id"])
        return {"ticket_id": str(ticket.id)}


@on("product.item.shipped", "ops.tell_customers_it_shipped")
async def on_shipped(ctx: BusContext) -> dict[str, Any] | None:
    """Everyone who asked for it hears it's live: one approval card per ticket, drafted from the release note."""
    async with ctx.services.db.tenant(ctx.business_id) as session:
        tickets = list(
            (
                await session.execute(
                    select(Ticket).where(
                        Ticket.product_item_id == uuid.UUID(ctx.data["item_id"]), Ticket.status != "solved"
                    )
                )
            ).scalars()
        )
        for t in tickets:
            text = f"Good news: what you asked for is now live. {ctx.data.get('note', '')}".strip()
            await decisions.propose(
                session,
                ctx.business_id,
                decisions.Proposal(
                    action_type=REPLY.key,
                    title=f"Tell them it shipped: “{t.subject[:120]}”",
                    summary=text[:500],
                    payload={"ticket_id": str(t.id), "text": text},
                    subject=("ticket", t.id),
                    requested_by="pack:ops.support",
                    priority=50,
                ),
            )
        return {"told": len(tickets)}


def ticket_view(t: Ticket) -> dict[str, Any]:
    return {
        **_t(t),
        "id": str(t.id),
        "subject": t.subject,
        "body": t.body,
        "channel": t.channel,
        "sentiment": t.sentiment,
        "summary": t.summary,
        "sla_due_at": t.sla_due_at.isoformat() if t.sla_due_at else None,
        "first_response_at": t.first_response_at.isoformat() if t.first_response_at else None,
        "solved_at": t.solved_at.isoformat() if t.solved_at else None,
        "reply_draft": t.reply_draft,
        "decision_id": str(t.decision_id) if t.decision_id else None,
        "product_item_id": str(t.product_item_id) if t.product_item_id else None,
        "csat": t.csat,
    }


def project_view(p: Project, tasks: list[Task]) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "name": p.name,
        "status": p.status,
        "hold_reason": p.hold_reason,
        "billing": p.billing,
        "deal_id": str(p.deal_id) if p.deal_id else None,
        "contact_id": str(p.contact_id) if p.contact_id else None,
        "started_on": p.started_on.isoformat(),
        "due_on": p.due_on.isoformat() if p.due_on else None,
        "kickoff_event_id": str(p.kickoff_event_id) if p.kickoff_event_id else None,
        "tasks": [
            {
                "id": str(t.id),
                "title": t.title,
                "due_on": t.due_on.isoformat() if t.due_on else None,
                "status": t.status,
                "milestone": t.milestone,
                "billable_minor": t.billable_minor,
            }
            for t in sorted(tasks, key=lambda x: x.position)
        ],
    }


async def numbers(session: AsyncSession, business_id: uuid.UUID) -> dict[str, Any]:
    now = utcnow()
    open_tickets = await session.scalar(select(func.count()).select_from(Ticket).where(Ticket.status == "open"))
    late = await session.scalar(
        select(func.count())
        .select_from(Ticket)
        .where(Ticket.status == "open", Ticket.first_response_at.is_(None), Ticket.sla_due_at < now)
    )
    active = await session.scalar(select(func.count()).select_from(Project).where(Project.status == "active"))
    csat = await session.scalar(select(func.avg(Ticket.csat)).where(Ticket.csat.is_not(None)))
    overdue_tasks = await session.scalar(
        select(func.count())
        .select_from(Task)
        .join(Project, Project.id == Task.project_id)
        .where(Task.status == "todo", Task.due_on < date.today(), Project.status == "active")
    )
    return {
        "open_tickets": int(open_tickets or 0),
        "tickets_past_sla": int(late or 0),
        "active_projects": int(active or 0),
        "overdue_tasks": int(overdue_tasks or 0),
        "csat": round(float(csat), 2) if csat is not None else None,
    }


@offline_generator("ops.triage")
def _offline_triage(req: ModelRequest) -> dict[str, Any]:
    text = str(req.context.get("text", "")).lower()
    kind = (
        "feature"
        if any(w in text for w in ("feature", "would love", "could you add", "wish"))
        else "bug"
        if any(w in text for w in ("bug", "broken", "error", "crash"))
        else "billing"
        if any(w in text for w in ("invoice", "refund", "charge"))
        else "question"
    )
    priority = "urgent" if any(w in text for w in ("urgent", "down", "can't log in", "cannot log in")) else "normal"
    return {"kind": kind, "priority": priority, "sentiment": "neutral", "summary": text.split("\n")[0][:120]}


@offline_generator("ops.answer")
def _offline_answer(req: ModelRequest) -> dict[str, Any]:
    article = str(req.context.get("article", ""))
    first = re.split(r"(?<=[.!?])\s+", article.strip())[0] if article.strip() else ""
    return {"can_answer": bool(first), "reply": f"Thanks for asking. {first}" if first else "", "quote": first}


@on("gtm.contact.erased", "ops.erase_contact")
async def on_erased(ctx: BusContext) -> dict[str, Any] | None:
    """Privacy: a person asked to be forgotten. Their ticket text goes; the counts and dates stay."""
    from sqlalchemy import update

    async with ctx.services.db.tenant(ctx.business_id) as session:
        result = await session.execute(
            update(Ticket)
            .where(Ticket.contact_id == uuid.UUID(ctx.data["id"]))
            .values(subject="[erased]", body="[erased]", summary="", reply_draft=None)
        )
        return {"erased_tickets": result.rowcount}  # type: ignore[attr-defined]
