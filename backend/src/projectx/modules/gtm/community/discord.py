"""The Discord community bot, over HTTP interactions (no always-on gateway connection to babysit).

Install: the owner adds the bot to their server from the cockpit. The OAuth code exchange proves which
server it was (we never trust a guild id from a query string), and maps that server to the business.

Commands:
  /ask <question>     → lands in the shared inbox. Common questions are answered in the brand voice
                         (same policy as every other channel); anything else goes to a person.
  /subscribe <email>  → newsletter signup with explicit consent recorded (double opt-in when enabled).
  /latest             → the latest published posts.

Every request is verified with the application's Ed25519 public key; unsigned or stale requests are refused.
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import timedelta
from typing import Any

import orjson
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from projectx.api.deps import OwnerDep
from projectx.kernel.errors import AppError, RateLimited, ValidationFailed
from projectx.modules.gtm.config import gtm_settings
from projectx.modules.gtm.crm.lifecycle import Cause, ConsentInput, upsert_contact
from projectx.modules.gtm.growth.landing import confirm_token
from projectx.modules.gtm.inbox.service import ingest
from projectx.modules.gtm.models import ChannelAccount, Post
from projectx.services import Services
from projectx.spine.identity.security import throttle
from projectx.spine.identity.service import consume_email_token, issue_email_token
from projectx.spine.ledger.service import Actor
from projectx.spine.lifecycle import queue_email
from projectx.spine.mail import render
from projectx.spine.routing.models import RoutingKey
from projectx.spine.tenancy.models import Business

API = "https://discord.com/api/v10"
router = APIRouter(prefix="/api/v1/b/{business_id}/gtm/discord", tags=["gtm: discord"])
public_router = APIRouter(include_in_schema=False)

# Interaction types and response types (Discord API).
PING, APPLICATION_COMMAND = 1, 2
PONG, MESSAGE = 1, 4
EPHEMERAL = 1 << 6
MAX_SKEW_S = 300
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# View channels, send messages, read history, embed links.
BOT_PERMISSIONS = 1024 | 2048 | 65536 | 16384

COMMANDS: list[dict[str, Any]] = [
    {
        "name": "ask",
        "description": "Ask a question; the team (or the brand assistant) answers here",
        "options": [
            {"type": 3, "name": "question", "description": "Your question", "required": True, "max_length": 1000}
        ],
    },
    {
        "name": "subscribe",
        "description": "Get the newsletter by email",
        "options": [
            {"type": 3, "name": "email", "description": "Your email address", "required": True, "max_length": 254}
        ],
    },
    {"name": "latest", "description": "See the latest posts"},
]


def verify_signature(
    public_key_hex: str, signature_hex: str, timestamp: str, body: bytes, now: float | None = None
) -> bool:
    try:
        if abs((now or time.time()) - int(timestamp)) > MAX_SKEW_S:
            return False
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex)).verify(
            bytes.fromhex(signature_hex), timestamp.encode() + body
        )
        return True
    except (InvalidSignature, ValueError):
        return False


def _reply(text: str, *, private: bool = True) -> JSONResponse:
    return JSONResponse(
        {
            "type": MESSAGE,
            "data": {"content": text[:2000], "flags": EPHEMERAL if private else 0, "allowed_mentions": {"parse": []}},
        }
    )


def _option(data: dict[str, Any], name: str) -> str:
    return str(next((o.get("value", "") for o in data.get("options", []) if o.get("name") == name), "")).strip()


# ---------------------------------------------------------------- install


@router.get("/install")
async def install_url(ctx: OwnerDep) -> dict[str, str]:
    cfg = ctx.services.settings
    if not cfg.discord_application_id or not cfg.discord_client_secret:
        raise ValidationFailed("The Discord app isn't configured on this server yet")
    state = await issue_email_token(
        ctx.session,
        user_id=ctx.user.id,
        purpose="discord_install",
        ttl=timedelta(minutes=15),
        payload={"business_id": str(ctx.business_id)},
    )
    from urllib.parse import urlencode

    query = urlencode(
        {
            "client_id": cfg.discord_application_id,
            "scope": "bot applications.commands",
            "permissions": str(BOT_PERMISSIONS),
            "response_type": "code",
            "redirect_uri": f"{cfg.base_url}/oauth/discord/bot/callback",
            "state": state,
        }
    )
    return {"url": f"https://discord.com/oauth2/authorize?{query}"}


@public_router.get("/oauth/discord/bot/callback")
async def install_callback(request: Request) -> RedirectResponse:
    services: Services = request.app.state.services
    cfg = services.settings
    web = cfg.web_url
    code, state = request.query_params.get("code"), request.query_params.get("state")
    if not code or not state or not cfg.discord_application_id or not cfg.discord_client_secret:
        return RedirectResponse(f"{web}/channels?error=denied", status_code=303)
    async with services.db.session() as session:
        try:
            token = await consume_email_token(session, state, "discord_install")
        except AppError:
            return RedirectResponse(f"{web}/channels?error=expired", status_code=303)
        business_id = uuid.UUID(str(token.payload["business_id"]))
    response = await services.http.post(
        f"{API}/oauth2/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": f"{cfg.base_url}/oauth/discord/bot/callback",
            "client_id": cfg.discord_application_id,
            "client_secret": cfg.discord_client_secret.get_secret_value(),
        },
    )
    guild = response.json().get("guild") if response.status_code < 400 else None
    if not guild or not guild.get("id"):
        return RedirectResponse(f"{web}/channels?error=connect_failed&platform=discord", status_code=303)
    async with services.db.tenant(business_id) as session:
        account = await session.scalar(
            select(ChannelAccount).where(
                ChannelAccount.platform == "discord", ChannelAccount.external_id == f"guild:{guild['id']}"
            )
        )
        if account is None:
            account = ChannelAccount(business_id=business_id, platform="discord", external_id=f"guild:{guild['id']}")
            session.add(account)
        account.name, account.status = f"{guild.get('name', 'Discord server')} (bot)", "connected"
        account.settings = {**(account.settings or {}), "provider": "discord_bot", "guild_id": guild["id"]}
        await session.flush()
        await session.execute(
            insert(RoutingKey)
            .values(namespace="bot", key=f"discord:{guild['id']}", business_id=business_id, target_id=account.id)
            .on_conflict_do_update(
                index_elements=[RoutingKey.namespace, RoutingKey.key],
                set_={"business_id": business_id, "target_id": account.id},
            )
        )
    return RedirectResponse(f"{web}/channels?connected=discord", status_code=303)


# ---------------------------------------------------------------- interactions


@public_router.post("/webhooks/discord/interactions")
async def interactions(request: Request) -> JSONResponse:
    services: Services = request.app.state.services
    key = services.settings.discord_public_key
    body = await request.body()
    signature = request.headers.get("x-signature-ed25519", "")
    timestamp = request.headers.get("x-signature-timestamp", "")
    if not key or not verify_signature(key, signature, timestamp, body):
        return JSONResponse({"error": "invalid request signature"}, status_code=401)
    payload = orjson.loads(body)
    if payload.get("type") == PING:
        return JSONResponse({"type": PONG})
    if payload.get("type") != APPLICATION_COMMAND:
        return _reply("That isn't supported.")
    guild_id = payload.get("guild_id")
    if not guild_id:
        return _reply("Use this command inside a server.")
    async with services.db.session() as session:
        route = await session.get(RoutingKey, ("bot", f"discord:{guild_id}"))
    if route is None:
        return _reply("This server isn't connected yet.")
    data = payload.get("data", {})
    member = payload.get("member") or {}
    user = member.get("user") or payload.get("user") or {}
    name = data.get("name")
    limits = {"subscribe": (3, timedelta(hours=1)), "ask": (10, timedelta(hours=1))}  # per person, per hour
    if name in limits:
        limit, window = limits[name]
        try:
            async with services.db.session() as session:
                await throttle(session, f"discord:{name}:{guild_id}:{user.get('id')}", limit=limit, window=window)
                await throttle(session, f"discord:{name}:{guild_id}", limit=limit * 30, window=window)
        except RateLimited:
            return _reply("That's a lot of requests. Please try again a little later.")
    async with services.db.tenant(route.business_id) as session:
        business = await session.get(Business, route.business_id)
        if business is None:
            return _reply("This server isn't connected yet.")
        if name == "ask":
            return await _ask(session, business, route.target_id, payload, data, user)
        if name == "subscribe":
            return await _subscribe(session, services, business, payload, data, user)
        if name == "latest":
            return await _latest(session, business)
    return _reply("Unknown command.")


async def _ask(
    session: Any,
    business: Business,
    account_id: uuid.UUID | None,
    payload: dict[str, Any],
    data: dict[str, Any],
    user: dict[str, Any],
) -> JSONResponse:
    question = _option(data, "question")
    if not question:
        return _reply("Add your question after /ask.")
    item = await ingest(
        session,
        business,
        platform="discord",
        kind="mention",
        external_id=f"interaction:{payload.get('id')}",
        text=question,
        author_handle=user.get("username"),
        author_name=user.get("global_name"),
        account_id=account_id,
        thread_id=str(payload.get("channel_id") or ""),
        meta={"channel_id": payload.get("channel_id"), "guild_id": payload.get("guild_id"), "user_id": user.get("id")},
    )
    if item is None:
        return _reply("Got it already. You'll get an answer here.")
    return _reply(f"Thanks! {business.name} will answer here shortly.", private=False)


async def _subscribe(
    session: Any,
    services: Services,
    business: Business,
    payload: dict[str, Any],
    data: dict[str, Any],
    user: dict[str, Any],
) -> JSONResponse:
    email = _option(data, "email").lower()
    if not EMAIL.match(email):
        return _reply("That doesn't look like an email address.")
    double_opt_in = bool(gtm_settings(business)["newsletter"].get("double_opt_in", True))
    evidence = {
        "source": "discord",
        "guild_id": payload.get("guild_id"),
        "discord_user_id": user.get("id"),
        "command": "/subscribe",
        "interaction_id": payload.get("id"),
    }
    consent = (
        None
        if double_opt_in
        else ConsentInput(channel="email", purpose="newsletter", source="discord", evidence=evidence)
    )
    contact, _ = await upsert_contact(
        session,
        business,
        source="discord",
        cause=Cause("discord", actor=Actor.public()),
        email=email,
        name=user.get("global_name") or user.get("username"),
        stage="subscriber",
        consent=consent,
    )
    contact.handles = {**(contact.handles or {}), "discord": str(user.get("username") or user.get("id"))}
    if double_opt_in:
        contact.attributes = {**(contact.attributes or {}), "pending_consent": evidence}
        token = confirm_token(
            services.settings.secret_key.get_secret_value(), business.id, contact.id, uuid.UUID(int=0)
        )
        link = f"{services.settings.base_url}/c/{token}"
        await queue_email(
            session,
            to=email,
            subject=f"Confirm your subscription to {business.name}",
            html=render(
                "email/action.html",
                title="One more step",
                name=contact.name or "there",
                body=f"Confirm you want emails from {business.name}.",
                action_label="Confirm subscription",
                action_url=link,
                app_name=business.name,
            ),
            text=f"Confirm your subscription to {business.name}: {link}",
            business_id=business.id,
        )
        return _reply(f"Almost there: check {email} to confirm.")
    return _reply("You're subscribed. Welcome!")


async def _latest(session: Any, business: Business) -> JSONResponse:
    posts = (
        (
            await session.execute(
                select(Post)
                .where(Post.business_id == business.id, Post.status == "published", Post.url.is_not(None))
                .order_by(Post.published_at.desc())
                .limit(3)
            )
        )
        .scalars()
        .all()
    )
    if not posts:
        return _reply("Nothing published yet. Check back soon!")
    return _reply("Latest from " + business.name + ":\n" + "\n".join(f"• {p.url}" for p in posts))


async def reply_in_channel(services: Services, item_meta: dict[str, Any], text: str) -> dict[str, Any]:
    """Answer an /ask in its channel, mentioning the person who asked."""
    token = services.settings.discord_bot_token
    if token is None or not item_meta.get("channel_id"):
        raise ValidationFailed("The Discord bot isn't configured")
    mention = f"<@{item_meta['user_id']}> " if item_meta.get("user_id") else ""
    response = await services.http.post(
        f"{API}/channels/{item_meta['channel_id']}/messages",
        json={
            "content": (mention + text)[:2000],
            "allowed_mentions": {"users": [item_meta["user_id"]] if item_meta.get("user_id") else []},
        },
        headers={"Authorization": f"Bot {token.get_secret_value()}"},
    )
    if response.status_code >= 400:
        from projectx.modules.gtm.channels.adapters.base import AdapterError, PlatformRejected

        if response.status_code >= 500 or response.status_code == 429:
            raise AdapterError(f"Discord: {response.text[:200]}")
        raise PlatformRejected(f"Discord refused the reply: {response.text[:200]}")
    return {"external_id": str(response.json().get("id"))}


async def register_commands(services: Services) -> int:
    """Install or update the slash commands (global). Run once per deploy: `projectx discord-commands`."""
    cfg = services.settings
    if not cfg.discord_application_id or cfg.discord_bot_token is None:
        raise ValidationFailed("Set PX_DISCORD_APPLICATION_ID and PX_DISCORD_BOT_TOKEN first")
    response = await services.http.put(
        f"{API}/applications/{cfg.discord_application_id}/commands",
        json=COMMANDS,
        headers={"Authorization": f"Bot {cfg.discord_bot_token.get_secret_value()}"},
    )
    response.raise_for_status()
    return len(response.json())
