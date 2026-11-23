"""Inbox endpoints and the platform webhooks that feed it."""

from __future__ import annotations

import hashlib
import hmac
import random
import uuid
from typing import Any

import orjson
from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from projectx.api.deps import BusinessDep, EditorDep
from projectx.kernel.errors import ValidationFailed
from projectx.kernel.time import utcnow
from projectx.modules.gtm.inbox.service import get_or_404, ingest, item_view
from projectx.modules.gtm.models import ChannelAccount, InboxItem, Post
from projectx.services import Services
from projectx.spine.decisions import service as decisions
from projectx.spine.decisions.service import Proposal, propose
from projectx.spine.routing.models import RoutingKey
from projectx.spine.tenancy.models import Business

router = APIRouter(prefix="/api/v1/b/{business_id}/gtm/inbox", tags=["gtm: inbox"])
public_router = APIRouter(include_in_schema=False)


@router.get("")
async def inbox(
    ctx: BusinessDep, status_filter: str | None = None, platform: str | None = None, limit: int = 100
) -> dict[str, Any]:
    stmt = select(InboxItem).where(InboxItem.business_id == ctx.business_id)
    if status_filter:
        stmt = stmt.where(InboxItem.status.in_(status_filter.split(",")))
    if platform:
        stmt = stmt.where(InboxItem.platform == platform)
    rows = (
        (
            await ctx.session.execute(
                stmt.order_by(InboxItem.priority, InboxItem.received_at.desc()).limit(min(limit, 300))
            )
        )
        .scalars()
        .all()
    )
    counts = dict(
        (
            await ctx.session.execute(
                select(InboxItem.status, func.count())
                .where(InboxItem.business_id == ctx.business_id)
                .group_by(InboxItem.status)
            )
        )
        .tuples()
        .all()
    )
    return {"items": [item_view(i) for i in rows], "counts": {k: int(v) for k, v in counts.items()}}


class ReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


@router.post("/{item_id}/reply")
async def reply_now(item_id: uuid.UUID, body: ReplyIn, ctx: EditorDep) -> dict[str, Any]:
    """A person writes (or edits) the reply: it goes out as their decision, through the same pipeline."""
    item = await get_or_404(ctx.session, item_id)
    if item.decision_id:
        await decisions.cancel(ctx.session, ctx.business_id, item.decision_id, reason="answered by a person")
    action = "gtm.reply_private" if item.kind == "dm" else "gtm.reply_public"
    outcome = await propose(
        ctx.session,
        ctx.business_id,
        Proposal(
            action_type=action,
            title=f"Reply to {item.author_handle or 'a comment'}",
            summary=item.text[:200],
            payload={"item_id": str(item.id), "text": body.text},
            subject=("inbox_item", item.id),
            requested_by=f"user:{ctx.user.id}",
        ),
    )
    if outcome.decision_id and not outcome.verdict.runs_now:
        await decisions.approve(ctx.session, ctx.business_id, outcome.decision_id, ctx.user.id)
    item.status, item.reply_text = "replying", body.text
    return item_view(item)


class StatusIn(BaseModel):
    status: str = Field(pattern=r"^(open|closed|escalated)$")


@router.post("/{item_id}/status")
async def set_status(item_id: uuid.UUID, body: StatusIn, ctx: EditorDep) -> dict[str, Any]:
    item = await get_or_404(ctx.session, item_id)
    item.status = body.status
    if body.status != "open":
        item.assigned_user_id = ctx.user.id
    return item_view(item)


_SAMPLES = [
    ("comment", "How often do you publish new stories?"),
    ("comment", "This was so helpful, thank you! 🔥"),
    ("dm", "Hi! What does it cost to sponsor a slot in your newsletter?"),
    ("comment", "This is misleading and honestly terrible reporting."),
    ("mention", "Loving the daily digest from these folks"),
    ("comment", "Free followers!!! DM me for crypto gains bit.ly/xyz"),
]


@router.post("/simulate", status_code=status.HTTP_201_CREATED)
async def simulate(ctx: EditorDep) -> dict[str, Any]:
    """Practice mode: drop realistic comments and DMs into the inbox to see triage work."""
    account = await ctx.session.scalar(
        select(ChannelAccount)
        .where(ChannelAccount.business_id == ctx.business_id, ChannelAccount.status == "practice")
        .limit(1)
    )
    if account is None:
        raise ValidationFailed("Connect a practice account first")
    post = await ctx.session.scalar(
        select(Post).where(Post.business_id == ctx.business_id, Post.account_id == account.id).limit(1)
    )
    created = 0
    for kind, text in random.sample(_SAMPLES, k=4):
        item = await ingest(
            ctx.session,
            ctx.business,
            platform=account.platform,
            kind=kind,
            external_id=f"sim_{uuid.uuid4().hex[:12]}",
            text=text,
            author_handle=f"reader_{uuid.uuid4().hex[:5]}",
            account_id=account.id,
            post_id=post.id if post else None,
        )
        created += int(item is not None)
    return {"created": created}


# ------------------------------------------------------------------ webhooks


async def _route(services: Services, platform: str, external_id: str) -> tuple[Business, uuid.UUID] | None:
    async with services.db.session() as session:
        route = await session.get(RoutingKey, ("account", f"{platform}:{external_id}"))
        business = await session.get(Business, route.business_id) if route else None
    return (business, route.target_id) if business and route and route.target_id else None


@public_router.get("/webhooks/meta")
async def meta_verify(request: Request) -> Response:
    services: Services = request.app.state.services
    token = services.settings.meta_webhook_verify_token
    params = request.query_params
    if (
        token
        and params.get("hub.mode") == "subscribe"
        and hmac.compare_digest(params.get("hub.verify_token", ""), token.get_secret_value())
    ):
        return PlainTextResponse(params.get("hub.challenge", ""))
    return PlainTextResponse("forbidden", status_code=403)


@public_router.post("/webhooks/meta")
async def meta_events(request: Request) -> Response:
    services: Services = request.app.state.services
    body = await request.body()
    secret = services.settings.meta_app_secret
    signature = request.headers.get("x-hub-signature-256", "")
    if secret is None or not hmac.compare_digest(
        "sha256=" + hmac.new(secret.get_secret_value().encode(), body, hashlib.sha256).hexdigest(), signature
    ):
        return JSONResponse({"error": "bad signature"}, status_code=401)
    payload = orjson.loads(body)
    if payload.get("object") == "whatsapp_business_account":
        return JSONResponse({"handled": await _whatsapp_events(services, payload)})
    platform = "instagram" if payload.get("object") == "instagram" else "facebook"
    handled = 0
    for entry in payload.get("entry", []):
        routed = await _route(services, platform, str(entry.get("id")))
        if routed is None:
            continue
        business, account_id = routed
        events: list[dict[str, Any]] = []
        for change in entry.get("changes", []):
            value = change.get("value", {})
            if (
                change.get("field") in ("comments", "feed")
                and value.get("item", "comment") == "comment"
                and value.get("verb", "add") == "add"
            ):
                author = value.get("from", {})
                events.append(
                    {
                        "kind": "comment",
                        "id": value.get("comment_id") or value.get("id"),
                        "text": value.get("message") or value.get("text", ""),
                        "handle": author.get("username") or author.get("name") or author.get("id"),
                    }
                )
            elif change.get("field") == "mentions":
                events.append(
                    {
                        "kind": "mention",
                        "id": value.get("media_id") or value.get("comment_id"),
                        "text": value.get("text", ""),
                        "handle": None,
                    }
                )
        for message in entry.get("messaging", []):
            if "message" in message and not message["message"].get("is_echo"):
                events.append(
                    {
                        "kind": "dm",
                        "id": message["message"].get("mid"),
                        "text": message["message"].get("text", ""),
                        "handle": message["sender"]["id"],
                        "thread": message["sender"]["id"],
                    }
                )
        async with services.db.tenant(business.id) as session:
            for e in events:
                if e["id"] and e["text"]:
                    await ingest(
                        session,
                        business,
                        platform=platform,
                        kind=e["kind"],
                        external_id=str(e["id"]),
                        text=e["text"],
                        author_handle=e.get("handle"),
                        account_id=account_id,
                        thread_id=e.get("thread"),
                    )
                    handled += 1
    return JSONResponse({"handled": handled})


async def _whatsapp_events(services: Services, payload: dict[str, Any]) -> int:
    from projectx.modules.gtm.channels.whatsapp import handle_webhook_value

    handled = 0
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "messages":
                continue
            value = change.get("value", {})
            number_id = str((value.get("metadata") or {}).get("phone_number_id", ""))
            routed = await _route(services, "whatsapp", number_id) if number_id else None
            if routed is None:
                continue
            business, account_id = routed
            async with services.db.tenant(business.id) as session:
                handled += await handle_webhook_value(session, business, account_id, value)
    return handled


@public_router.post("/webhooks/telegram/{account_id}")
async def telegram_events(account_id: uuid.UUID, request: Request) -> Response:
    services: Services = request.app.state.services
    secret = services.settings.telegram_webhook_secret
    if secret is None or not hmac.compare_digest(
        request.headers.get("x-telegram-bot-api-secret-token", ""), secret.get_secret_value()
    ):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    update = orjson.loads(await request.body())
    message = update.get("message") or update.get("channel_post") or {}
    if not message.get("text"):
        return JSONResponse({"ignored": True})
    async with services.db.session() as session:
        route = await session.scalar(
            select(RoutingKey).where(RoutingKey.namespace == "account", RoutingKey.target_id == account_id)
        )
        business = await session.get(Business, route.business_id) if route else None
    if business is None:
        return JSONResponse({"ignored": True})
    author = message.get("from", {})
    async with services.db.tenant(business.id) as session:
        await ingest(
            session,
            business,
            platform="telegram",
            kind="reply" if message.get("reply_to_message") else "dm",
            external_id=f"{message['chat']['id']}:{message['message_id']}",
            text=message["text"],
            author_handle=author.get("username") or str(author.get("id", "")) or None,
            author_name=" ".join(filter(None, [author.get("first_name"), author.get("last_name")])) or None,
            account_id=account_id,
            thread_id=str(message["chat"]["id"]),
            received_at=utcnow(),
        )
    return JSONResponse({"ok": True})
