"""Product at work: turn what customers ask for into a ranked roadmap, spec the work, and close the loop.

- Feedback (Operations) → an open model matches the ticket to an existing roadmap item or names a new one; code
  checks the match is a real item, links the ticket, and re-ranks: score = (people who asked + their value) / effort.
- A spec for an item is a hard job (it is engineering work), so it is the one place here that may use Claude.
- A release marks its items shipped: Operations tells every customer who asked, GTM drafts the announcement.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.errors import NotFound, ValidationFailed
from projectx.kernel.time import utcnow
from projectx.modules.product.models import FeedbackLink, Item, Release
from projectx.spine.bus import BusContext, on
from projectx.spine.gateway.contracts import ChatMessage, GatewayError, ModelRequest
from projectx.spine.gateway.offline import offline_generator
from projectx.spine.jobs.queue import enqueue
from projectx.spine.jobs.registry import JobContext, job
from projectx.spine.ledger.events import EventData, register_event
from projectx.spine.ledger.service import Actor, append
from projectx.spine.twin.models import Contact

AGENT = Actor.agent("product")


class Linked(EventData):
    ticket_id: str
    item_id: str
    created_item: bool


class ItemShipped(EventData):
    item_id: str
    release_id: str
    note: str


class ReleasePublished(EventData):
    release_id: str
    version: str
    items: int


for _k, _d, _s in (
    ("product.feedback.linked", "Customer feedback was linked to a roadmap item", Linked),
    ("product.item.shipped", "A roadmap item shipped", ItemShipped),
    ("product.release.published", "A release went out", ReleasePublished),
):
    register_event(_k, _d, _s)


def score(requesters: int, value_minor: int, effort: int) -> float:
    """Plain and explainable: each asker counts 10, plus 1 per 1,000 minor units of their value, over effort."""
    return round((requesters * 10 + value_minor / 1000) / max(effort, 1), 2)


class Match(BaseModel):
    item_id: str = Field(description="The id of the matching item from the list, or empty if none matches")
    new_title: str = Field(description="If none matches: a short title for a new item; else empty")


async def rerank(session: AsyncSession, item: Item) -> None:
    contacts = list(
        (
            await session.execute(
                select(FeedbackLink.contact_id)
                .where(FeedbackLink.item_id == item.id, FeedbackLink.contact_id.is_not(None))
                .distinct()
            )
        ).scalars()
    )
    tickets = await session.scalar(
        select(func.count()).select_from(FeedbackLink).where(FeedbackLink.item_id == item.id)
    )
    value = (
        await session.scalar(
            select(func.coalesce(func.sum(Contact.lifetime_value_minor), 0)).where(Contact.id.in_(contacts))
        )
        if contacts
        else 0
    )
    item.requesters = len(contacts) or int(tickets or 0)  # distinct customers; tickets if none are known
    item.value_minor = int(value or 0)
    item.score = score(item.requesters, item.value_minor, item.effort)


@on("ops.feedback.received", "product.link_feedback")
async def on_feedback(ctx: BusContext) -> dict[str, Any] | None:
    from projectx.modules.ops.models import Ticket

    async with ctx.services.db.tenant(ctx.business_id) as session:
        ticket = await session.get(Ticket, uuid.UUID(ctx.data["ticket_id"]))
        if ticket is None:
            return {"skipped": "ticket gone"}
        if await session.scalar(select(FeedbackLink.id).where(FeedbackLink.ticket_id == ticket.id)):
            return {"skipped": "already linked"}
        open_items = list(
            (
                await session.execute(
                    select(Item)
                    .where(Item.kind == ctx.data["kind"], Item.status.in_(("idea", "planned", "building")))
                    .limit(60)
                )
            ).scalars()
        )
        listing = "\n".join(f"- {i.id}: {i.title} ({i.description[:200]})" for i in open_items) or "- (none yet)"
        ask = ticket.body[:1500].strip()
        text = f"{ticket.subject}\n{ask}"
    try:
        match = (
            await ctx.services.gateway.run(
                ctx.business_id,
                ModelRequest(
                    task="product.match",
                    system="You keep a product roadmap tidy. Match the customer's request to an existing item "
                    "only if it asks for the same thing; otherwise propose a short title for a new item.",
                    messages=[ChatMessage("user", f"ITEMS:\n{listing}\n\nREQUEST:\n{text}")],
                    output_schema=Match,
                    max_tokens=200,
                    rung="small",
                    max_rung="mid",
                    context={"text": ask, "items": [[str(i.id), f"{i.title} {i.description}"] for i in open_items]},
                ),
            )
        ).parsed_as(Match)
    except GatewayError:
        match = Match(item_id="", new_title=ticket.subject[:200])
    async with ctx.services.db.tenant(ctx.business_id) as session:
        valid = {str(i.id) for i in open_items}
        created = False
        if match.item_id in valid:
            item = await session.get(Item, uuid.UUID(match.item_id), with_for_update=True)
            assert item is not None
        else:  # an unknown id is never trusted: it becomes a new item instead
            title = (match.new_title or ask.split("\n", maxsplit=1)[0] or ticket.subject)[:200]
            item = Item(business_id=ctx.business_id, title=title, kind=ctx.data["kind"], description=ask[:2000])
            session.add(item)
            await session.flush()
            created = True
        session.add(
            FeedbackLink(
                business_id=ctx.business_id,
                ticket_id=uuid.UUID(ctx.data["ticket_id"]),
                item_id=item.id,
                contact_id=uuid.UUID(ctx.data["contact_id"]) if ctx.data.get("contact_id") else None,
            )
        )
        await session.flush()
        await rerank(session, item)
        await append(
            session,
            business_id=ctx.business_id,
            type="product.feedback.linked",
            actor=AGENT,
            subject=("product_item", item.id),
            data={"ticket_id": ctx.data["ticket_id"], "item_id": str(item.id), "created_item": created},
            **ctx.cause,
        )
        return {"item_id": str(item.id), "created": created}


# ---------------------------------------------------------------- specs: the hard job


class Spec(BaseModel):
    problem: str
    proposal: str
    acceptance_criteria: list[str]
    test_plan: list[str]
    risks: list[str]


@job("product.write_spec", max_attempts=3, timeout_s=600)
async def write_spec(ctx: JobContext) -> dict[str, Any]:
    business_id = ctx.require_business()
    async with ctx.services.db.tenant(business_id) as session:
        item = await session.get(Item, uuid.UUID(ctx.payload["item_id"]))
        if item is None:
            return {"skipped": "gone"}
        from projectx.modules.ops.models import Ticket

        asks = (
            await session.execute(
                select(Ticket.summary)
                .join(FeedbackLink, FeedbackLink.ticket_id == Ticket.id)
                .where(FeedbackLink.item_id == item.id)
                .limit(20)
            )
        ).scalars()
        brief = f"{item.kind}: {item.title}\n{item.description}\n\nWhat customers said:\n" + "\n".join(
            f"- {a}" for a in asks if a
        )
    try:
        spec = (
            await ctx.services.gateway.run(
                business_id,
                ModelRequest(
                    task="product.spec",
                    system="You are a senior engineer writing a short, buildable spec: the problem, the smallest "
                    "change that solves it, testable acceptance criteria, a test plan and the risks.",
                    messages=[ChatMessage("user", brief)],
                    output_schema=Spec,
                    max_tokens=4000,
                    effort="high",
                    rung="frontier",
                    hard=True,  # engineering work: the hard-jobs model (Claude) if configured
                    context={"text": brief, "title": item.title},
                ),
            )
        ).parsed_as(Spec)
    except GatewayError as exc:
        async with ctx.services.db.tenant(business_id) as session:
            item = await session.get(Item, uuid.UUID(ctx.payload["item_id"]))
            if item is not None:
                item.spec_status = "none"
        return {"failed": str(exc)[:200]}
    text = (
        f"Problem\n{spec.problem}\n\nProposal\n{spec.proposal}\n\nAcceptance criteria\n"
        + "\n".join(f"- {c}" for c in spec.acceptance_criteria)
        + "\n\nTest plan\n"
        + "\n".join(f"- {t}" for t in spec.test_plan)
        + "\n\nRisks\n"
        + "\n".join(f"- {r}" for r in spec.risks)
    )
    async with ctx.services.db.tenant(business_id) as session:
        item = await session.get(Item, uuid.UUID(ctx.payload["item_id"]), with_for_update=True)
        if item is not None:
            item.spec, item.spec_status = text, "ready"
    return {"spec": "ready"}


async def request_spec(session: AsyncSession, business_id: uuid.UUID, item: Item) -> None:
    item.spec_status = "drafting"
    await enqueue(
        session,
        "product.write_spec",
        {"item_id": str(item.id)},
        business_id=business_id,
        idempotency_key=f"spec:{item.id}:{utcnow():%Y%m%d%H%M%S}",
    )


# ---------------------------------------------------------------- releases


async def publish_release(
    session: AsyncSession, business_id: uuid.UUID, *, version: str, notes: str, item_ids: list[uuid.UUID], actor: Actor
) -> Release:
    items = list((await session.execute(select(Item).where(Item.id.in_(item_ids)).with_for_update())).scalars())
    if len(items) != len(set(item_ids)):
        raise NotFound("Some items don't exist")
    if not notes.strip():
        raise ValidationFailed("A release needs notes people can read")
    release = Release(
        business_id=business_id,
        version=version,
        notes=notes,
        item_ids=[str(i.id) for i in items],
        published_at=utcnow(),
    )
    session.add(release)
    await session.flush()
    for item in items:
        if item.status == "shipped":
            continue
        item.status, item.shipped_at = "shipped", utcnow()
        await append(
            session,
            business_id=business_id,
            type="product.item.shipped",
            actor=actor,
            subject=("product_item", item.id),
            data={"item_id": str(item.id), "release_id": str(release.id), "note": notes[:300]},
        )
    await append(
        session,
        business_id=business_id,
        type="product.release.published",
        actor=actor,
        subject=("release", release.id),
        data={"release_id": str(release.id), "version": version, "items": len(items)},
    )
    return release


def item_view(i: Item) -> dict[str, Any]:
    return {
        "id": str(i.id),
        "title": i.title,
        "kind": i.kind,
        "description": i.description,
        "status": i.status,
        "effort": i.effort,
        "requesters": i.requesters,
        "value_minor": i.value_minor,
        "score": i.score,
        "spec": i.spec,
        "spec_status": i.spec_status,
        "shipped_at": i.shipped_at.isoformat() if i.shipped_at else None,
    }


async def numbers(session: AsyncSession, business_id: uuid.UUID) -> dict[str, Any]:
    open_items = await session.scalar(
        select(func.count()).select_from(Item).where(Item.status.in_(("idea", "planned", "building")))
    )
    bugs = await session.scalar(
        select(func.count()).select_from(Item).where(Item.kind == "bug", Item.status != "shipped")
    )
    top = await session.scalar(
        select(Item.title).where(Item.status.in_(("idea", "planned"))).order_by(Item.score.desc()).limit(1)
    )
    return {"open_items": int(open_items or 0), "open_bugs": int(bugs or 0), "top_item": top}


@offline_generator("product.match")
def _offline_match(req: ModelRequest) -> dict[str, Any]:
    import re

    text = str(req.context.get("text", "")).lower()
    words = set(re.findall(r"[a-z]{4,}", text))
    best, overlap = "", 0
    for item_id, title in req.context.get("items", []):
        o = len(words & set(re.findall(r"[a-z]{4,}", title.lower())))
        if o > overlap:
            best, overlap = item_id, o
    if overlap >= 2:
        return {"item_id": best, "new_title": ""}
    return {"item_id": "", "new_title": text.split("\n")[0][:120].strip().capitalize()}


@offline_generator("product.spec")
def _offline_spec(req: ModelRequest) -> dict[str, Any]:
    title = str(req.context.get("title", "the change"))
    return {
        "problem": f"Customers asked for: {title}.",
        "proposal": "Draft (no model connected): describe the smallest change that solves it.",
        "acceptance_criteria": [f"{title} works for a customer end to end"],
        "test_plan": ["Add an automated test for the main path"],
        "risks": ["Unknown until reviewed by an engineer"],
    }
