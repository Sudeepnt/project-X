"""The shared inbox: comments, DMs, mentions and replies from every channel, in one queue.

AI drafts replies. It only answers common questions on its own (and only once the action type has
earned it); anything negative or sensitive goes to a person. Likes, follows and mass DMs are never
automated.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.errors import NotFound, PolicyBlocked
from projectx.kernel.time import utcnow
from projectx.modules.gtm.brand.service import current_book
from projectx.modules.gtm.config import gtm_settings
from projectx.modules.gtm.content.generate import brand_system_prompt
from projectx.modules.gtm.crm.lifecycle import Cause, record_touch, upsert_contact
from projectx.modules.gtm.models import ChannelAccount, InboxItem
from projectx.spine.decisions.executors import ExecutionContext, ExecutionResult, executor
from projectx.spine.decisions.service import Proposal, propose
from projectx.spine.gateway.contracts import ChatMessage, ComputeCapReached, ModelRequest
from projectx.spine.gateway.offline import offline_generator
from projectx.spine.jobs.queue import enqueue
from projectx.spine.jobs.registry import JobContext, RetryLater, job
from projectx.spine.ledger.service import Actor
from projectx.spine.notifications.service import notify
from projectx.spine.policy.engine import Violation
from projectx.spine.tenancy.models import Business

_SPAM = re.compile(
    r"(crypto|forex|dm me for|click here|free followers|onlyfans|\bbit\.ly/|promo code\s+\w+\s+at)", re.I
)
_NEGATIVE = re.compile(
    r"(scam|fraud|terrible|worst|awful|refund|lawsuit|disgusting|hate|angry|never again|useless|"
    r"liar|fake news|misleading)",
    re.I,
)
_SENSITIVE = re.compile(
    r"(suicide|self[- ]harm|abuse|harass|legal action|lawyer|police|medical|diagnos|politic|"
    r"religio|threat)",
    re.I,
)
_SALES = re.compile(
    r"(price|pricing|cost|how much|buy|quote|hire|available|sponsor|advertis|rate card|media kit)", re.I
)
_PRAISE = re.compile(r"(love|great|amazing|thanks|thank you|awesome|brilliant|helpful|🔥|❤️|👏)", re.I)


class Triage(BaseModel):
    category: str = Field(
        description="common_question | sales_intent | support | praise | negative | sensitive | spam | other"
    )
    sentiment: float = Field(ge=-1, le=1)
    confidence: float = Field(ge=0, le=1)
    reply: str = Field(max_length=1000)


def rule_triage(text: str) -> tuple[str, float, float] | None:
    """Fast deterministic triage for the clear cases; None means 'ask the model'."""
    if _SPAM.search(text):
        return "spam", -0.2, 0.95
    if _SENSITIVE.search(text):
        return "sensitive", -0.4, 0.9
    if _NEGATIVE.search(text):
        return "negative", -0.7, 0.85
    return None


@offline_generator("gtm.inbox.triage")
def _offline_triage(req: ModelRequest) -> dict[str, Any]:
    text = str(req.context.get("text", ""))
    name = req.context.get("business_name", "us")
    if _SALES.search(text):
        return {
            "category": "sales_intent",
            "sentiment": 0.3,
            "confidence": 0.8,
            "reply": "Thanks for asking! We'll send you the details by DM.",
        }
    if "?" in text:
        return {
            "category": "common_question",
            "sentiment": 0.1,
            "confidence": 0.7,
            "reply": f"Good question! Here's the short answer from {name}: we'll share more in our next post.",
        }
    if _PRAISE.search(text):
        return {"category": "praise", "sentiment": 0.8, "confidence": 0.85, "reply": "Thank you, that means a lot!"}
    return {"category": "other", "sentiment": 0.0, "confidence": 0.5, "reply": "Thanks for sharing!"}


async def ingest(
    session: AsyncSession,
    business: Business,
    *,
    platform: str,
    kind: str,
    external_id: str,
    text: str,
    author_handle: str | None = None,
    author_name: str | None = None,
    account_id: uuid.UUID | None = None,
    post_id: uuid.UUID | None = None,
    thread_id: str | None = None,
    received_at: datetime | None = None,
    meta: dict[str, Any] | None = None,
) -> InboxItem | None:
    stmt = (
        insert(InboxItem)
        .values(
            id=uuid.uuid4(),
            business_id=business.id,
            platform=platform,
            kind=kind,
            external_id=external_id[:200],
            thread_id=thread_id,
            post_id=post_id,
            account_id=account_id,
            author_handle=author_handle,
            author_name=author_name,
            text=text[:10_000],
            received_at=received_at or utcnow(),
            status="open",
            meta=meta or {},
        )
        .on_conflict_do_nothing(index_elements=[InboxItem.business_id, InboxItem.platform, InboxItem.external_id])
    )
    item_id = await session.scalar(stmt.returning(InboxItem.id))
    if item_id is None:
        return None
    item = await session.get(InboxItem, item_id)
    assert item is not None
    if author_handle and kind in ("comment", "dm", "mention", "reply"):
        contact, _ = await upsert_contact(
            session,
            business,
            source=kind,
            cause=Cause("inbox", item.id),
            handle=(platform, author_handle),
            name=author_name,
            stage="lead",
        )
        item.contact_id = contact.id
        await record_touch(
            session,
            business,
            contact,
            kind="dm" if kind == "dm" else "comment",
            channel=platform,
            post_id=post_id,
            data={"inbox_item_id": str(item.id)},
            cause=Cause("inbox", item.id),
        )
    await enqueue(
        session,
        "gtm.triage_inbox",
        {"item_id": str(item.id)},
        business_id=business.id,
        idempotency_key=f"triage:{item.id}",
    )
    return item


@job("gtm.triage_inbox", max_attempts=4, timeout_s=120, priority=25)
async def triage_inbox(ctx: JobContext) -> dict[str, Any] | None:
    business_id = ctx.require_business()
    item_id = uuid.UUID(ctx.payload["item_id"])
    async with ctx.services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        item = await session.get(InboxItem, item_id)
        if business is None or item is None or item.status != "open":
            return None
        book = await current_book(session, business_id)
        text, platform, kind = item.text, item.platform, item.kind
    quick = rule_triage(text)
    reply = ""
    if quick is None:
        try:
            result = await ctx.services.gateway.run(
                business_id,
                ModelRequest(
                    task="gtm.inbox.triage",
                    system=brand_system_prompt(business, book) + "\nYou triage a comment or message and draft a short, "
                    "kind, specific reply in the brand voice. Never promise refunds, prices or anything legal.",
                    messages=[ChatMessage("user", f"{kind} on {platform}: {text}")],
                    output_schema=Triage,
                    max_tokens=800,
                    effort="low",
                    rung="small",
                    context={"text": text, "business_name": business.name},
                ),
            )
        except ComputeCapReached as exc:
            raise RetryLater(3600, "compute cap") from exc
        triage = result.parsed_as(Triage)
        category, sentiment, confidence, reply = triage.category, triage.sentiment, triage.confidence, triage.reply
    else:
        category, sentiment, confidence = quick
    async with ctx.services.db.tenant(business_id) as session:
        item = await session.get(InboxItem, item_id)
        business = await session.get(Business, business_id)
        assert item is not None
        assert business is not None
        item.category, item.sentiment, item.ai_draft = category, sentiment, reply or None
        if category == "spam":
            item.status = "closed"
            return {"category": category}
        if category in ("negative", "sensitive"):
            item.status, item.priority = "escalated", 10
            await notify(
                session,
                business_id,
                kind="inbox.escalated",
                level="warning",
                title=f"A {category} {kind} needs you",
                body=text[:200],
                link=f"/inbox?item={item.id}",
            )
            return {"category": category}
        threshold = float(gtm_settings(business)["inbox"].get("auto_reply_confidence", 0.85))
        violations: tuple[Violation, ...] = ()
        if category != "common_question" or confidence < threshold:
            violations = (
                Violation("needs_person", "Only common questions are ever answered automatically", hard=False),
            )
        action = "gtm.reply_private" if kind == "dm" else "gtm.reply_public"
        outcome = await propose(
            session,
            business_id,
            Proposal(
                action_type=action,
                title=f"Reply to {item.author_handle or 'a comment'} on {platform}",
                summary=text[:200],
                payload={"item_id": str(item.id), "text": reply},
                preview={"incoming": text, "reply": reply, "category": category},
                violations=violations,
                subject=("inbox_item", item.id),
                requested_by="pack:gtm.inbox",
                priority=30,
            ),
        )
        item.decision_id = outcome.decision_id
        item.status = "drafted" if not outcome.verdict.runs_now else "replying"
    return {"category": category, "status": outcome.status}


async def send_reply(services: Any, business_id: uuid.UUID, item_id: uuid.UUID, text: str) -> dict[str, Any]:
    from projectx.modules.gtm.channels.publisher import adapter_context
    from projectx.modules.gtm.channels.replies import reply

    async with services.db.tenant(business_id) as session:
        item = await session.get(InboxItem, item_id)
        if item is None:
            raise NotFound("Message not found")
        account = await session.get(ChannelAccount, item.account_id) if item.account_id else None
        if account is None:
            raise PolicyBlocked("This message isn't linked to a connected account")
        actx = await adapter_context(services, session, account, f"reply to {item.external_id}")
        platform, external_id, kind, thread = item.platform, item.external_id, item.kind, item.thread_id
        meta = dict(item.meta or {})
    if platform == "discord" and meta.get("channel_id"):
        from projectx.modules.gtm.community.discord import reply_in_channel

        result = await reply_in_channel(services, meta, text)
    else:
        result = await reply(
            services, account, actx, platform=platform, kind=kind, target_id=external_id, thread_id=thread, text=text
        )
    async with services.db.tenant(business_id) as session:
        item = await session.get(InboxItem, item_id)
        assert item is not None
        item.status, item.reply_text, item.replied_at = "replied", text, utcnow()
    return result


@executor("gtm.reply_public")
async def _reply_public(ctx: ExecutionContext) -> ExecutionResult:
    result = await send_reply(ctx.services, ctx.business_id, uuid.UUID(ctx.payload["item_id"]), ctx.payload["text"])
    return ExecutionResult(
        data=result, external_ref=result.get("external_id"), undo_payload={"reply_id": result.get("external_id")}
    )


@executor("gtm.reply_private")
async def _reply_private(ctx: ExecutionContext) -> ExecutionResult:
    result = await send_reply(ctx.services, ctx.business_id, uuid.UUID(ctx.payload["item_id"]), ctx.payload["text"])
    return ExecutionResult(data=result, external_ref=result.get("external_id"))


def item_view(i: InboxItem) -> dict[str, Any]:
    return {
        "id": str(i.id),
        "platform": i.platform,
        "kind": i.kind,
        "external_id": i.external_id,
        "author_handle": i.author_handle,
        "author_name": i.author_name,
        "contact_id": str(i.contact_id) if i.contact_id else None,
        "text": i.text,
        "received_at": i.received_at.isoformat(),
        "sentiment": i.sentiment,
        "category": i.category,
        "priority": i.priority,
        "status": i.status,
        "ai_draft": i.ai_draft,
        "reply_text": i.reply_text,
        "replied_at": i.replied_at.isoformat() if i.replied_at else None,
        "decision_id": str(i.decision_id) if i.decision_id else None,
        "post_id": str(i.post_id) if i.post_id else None,
    }


async def get_or_404(session: AsyncSession, item_id: uuid.UUID) -> InboxItem:
    item = await session.get(InboxItem, item_id)
    if item is None:
        raise NotFound("Message not found")
    return item


__all__ = ["Actor", "get_or_404", "ingest", "item_view", "rule_triage", "send_reply"]
