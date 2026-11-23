"""Connecting platform accounts.

Three ways in: a practice account (simulator, works today), a token (Telegram bot, Discord webhook,
Ayrshare profile) or OAuth 2.0 with PKCE where the platform supports it. Tokens go straight into the
vault with the narrowest scopes that work; the account row only points at the secret.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.errors import Conflict, NotConfigured, ValidationFailed
from projectx.kernel.time import utcnow
from projectx.modules.gtm.channels.adapters import adapter_for
from projectx.modules.gtm.channels.adapters.base import AccountInfo, AdapterContext, AdapterError
from projectx.modules.gtm.models import ChannelAccount
from projectx.modules.gtm.platforms import PLATFORMS
from projectx.services import Services
from projectx.settings import Settings
from projectx.spine.billing.plans import check_quota
from projectx.spine.jobs.registry import periodic
from projectx.spine.ledger.service import Actor, append
from projectx.spine.routing.models import RoutingKey
from projectx.spine.tenancy.models import Business

TOKEN_PLATFORMS = {"telegram": ("bot_token",), "discord": ("webhook_url",), "whatsapp": ("access_token",)}


@dataclass(frozen=True)
class OAuthConfig:
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    client_id: str | None
    client_secret: str | None
    pkce: bool = True
    basic_auth: bool = False
    client_id_param: str = "client_id"
    scope_separator: str = " "
    extra: tuple[tuple[str, str], ...] = ()


def _secret(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value else None


def oauth_config(platform: str, s: Settings) -> OAuthConfig:
    v = s.meta_graph_version
    meta_secret = _secret(s.meta_app_secret)
    configs = {
        "x": OAuthConfig(
            "https://x.com/i/oauth2/authorize",
            "https://api.x.com/2/oauth2/token",
            ("tweet.read", "tweet.write", "users.read", "offline.access"),
            s.x_client_id,
            _secret(s.x_client_secret),
            basic_auth=True,
        ),
        "linkedin": OAuthConfig(
            "https://www.linkedin.com/oauth/v2/authorization",
            "https://www.linkedin.com/oauth/v2/accessToken",
            ("openid", "profile", "w_member_social"),
            s.linkedin_client_id,
            _secret(s.linkedin_client_secret),
            pkce=False,
        ),
        "facebook": OAuthConfig(
            f"https://www.facebook.com/{v}/dialog/oauth",
            f"https://graph.facebook.com/{v}/oauth/access_token",
            (
                "pages_show_list",
                "pages_manage_posts",
                "pages_read_engagement",
                "pages_manage_engagement",
                "pages_messaging",
            ),
            s.meta_app_id,
            meta_secret,
            pkce=False,
            scope_separator=",",
        ),
        "instagram": OAuthConfig(
            f"https://www.facebook.com/{v}/dialog/oauth",
            f"https://graph.facebook.com/{v}/oauth/access_token",
            (
                "instagram_basic",
                "instagram_content_publish",
                "instagram_manage_insights",
                "instagram_manage_comments",
                "pages_show_list",
                "pages_read_engagement",
            ),
            s.meta_app_id,
            meta_secret,
            pkce=False,
            scope_separator=",",
        ),
        "threads": OAuthConfig(
            "https://threads.net/oauth/authorize",
            "https://graph.threads.net/oauth/access_token",
            (
                "threads_basic",
                "threads_content_publish",
                "threads_manage_insights",
                "threads_manage_replies",
                "threads_read_replies",
            ),
            s.threads_app_id,
            _secret(s.threads_app_secret),
            pkce=False,
            scope_separator=",",
        ),
        "tiktok": OAuthConfig(
            "https://www.tiktok.com/v2/auth/authorize/",
            "https://open.tiktokapis.com/v2/oauth/token/",
            ("user.info.basic", "video.publish", "video.list"),
            s.tiktok_client_key,
            _secret(s.tiktok_client_secret),
            client_id_param="client_key",
            scope_separator=",",
        ),
        "youtube": OAuthConfig(
            "https://accounts.google.com/o/oauth2/v2/auth",
            "https://oauth2.googleapis.com/token",
            ("https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"),
            s.google_client_id,
            _secret(s.google_client_secret),
            extra=(("access_type", "offline"), ("prompt", "consent")),
        ),
        "pinterest": OAuthConfig(
            "https://www.pinterest.com/oauth/",
            "https://api.pinterest.com/v5/oauth/token",
            ("boards:read", "pins:read", "pins:write", "user_accounts:read"),
            s.pinterest_app_id,
            _secret(s.pinterest_app_secret),
            pkce=False,
            basic_auth=True,
            scope_separator=",",
        ),
    }
    config = configs.get(platform)
    if config is None:
        raise ValidationFailed(f"{platform} doesn't connect with OAuth")
    if not config.client_id or not config.client_secret:
        raise NotConfigured(
            f"The {PLATFORMS[platform].label} app isn't set up yet. Add its credentials to the "
            "environment once the platform approves the app."
        )
    return config


def redirect_uri(s: Settings, platform: str) -> str:
    return f"{s.base_url}/oauth/{platform}/callback"


async def check_account_limit(session: AsyncSession, business: Business, platform: str) -> None:
    """Plans limit how many platforms you use, not how many accounts on a platform you already use."""
    already = await session.scalar(
        select(ChannelAccount.id)
        .where(
            ChannelAccount.business_id == business.id,
            ChannelAccount.platform == platform,
            ChannelAccount.status.in_(("connected", "practice", "expired")),
        )
        .limit(1)
    )
    if already is None:
        await check_quota(session, business, "platforms")


async def _live_elsewhere(session: AsyncSession, other: uuid.UUID, account_id: uuid.UUID | None) -> bool:
    """Is that account still connected in its business? Row-level security hides other tenants' rows, so
    look through that tenant's lens for one yes/no answer, then put ours back, in the same transaction."""
    from sqlalchemy import text as sql

    if account_id is None:
        return False
    ours = await session.scalar(sql("SELECT current_setting('app.business_id', true)"))
    try:
        await session.execute(sql("SELECT set_config('app.business_id', :b, true)"), {"b": str(other)})
        live = await session.scalar(
            select(ChannelAccount.id).where(
                ChannelAccount.id == account_id, ChannelAccount.status.in_(("connected", "practice", "expired"))
            )
        )
    finally:
        await session.execute(sql("SELECT set_config('app.business_id', :b, true)"), {"b": ours or ""})
    return live is not None


async def claim_route(
    session: AsyncSession, business: Business, platform: str, external_id: str, account_id: uuid.UUID
) -> None:
    """Point inbound webhooks for this platform account at this business.

    One platform account belongs to one business. If another business has it connected, refuse; if that
    connection was removed, take the route over (otherwise its comments would keep going to the old owner).
    """
    key = f"{platform}:{external_id}"
    existing = await session.get(RoutingKey, ("account", key))
    if (
        existing is not None
        and existing.business_id != business.id
        and await _live_elsewhere(session, existing.business_id, existing.target_id)
    ):
        raise Conflict("This account is already connected to another business on Project X")
    await session.execute(
        insert(RoutingKey)
        .values(namespace="account", key=key, business_id=business.id, target_id=account_id)
        .on_conflict_do_update(
            index_elements=[RoutingKey.namespace, RoutingKey.key],
            set_={"business_id": business.id, "target_id": account_id},
        )
    )


async def save_account(
    session: AsyncSession,
    services: Services,
    business: Business,
    *,
    platform: str,
    info: AccountInfo,
    credentials: dict[str, Any] | None,
    settings: dict[str, Any],
    status: str,
    scopes: list[str],
    expires_in: int | None,
    user_id: uuid.UUID,
) -> ChannelAccount:
    account = await session.scalar(
        select(ChannelAccount).where(
            ChannelAccount.business_id == business.id,
            ChannelAccount.platform == platform,
            ChannelAccount.external_id == info.external_id,
        )
    )
    if account is None:
        await check_account_limit(session, business, platform)
        account = ChannelAccount(
            business_id=business.id, platform=platform, external_id=info.external_id, name=info.name, settings={}
        )
        session.add(account)
        await session.flush()
    account.name, account.handle, account.avatar_url = info.name, info.handle, info.avatar_url
    account.followers = info.followers
    account.capabilities = {**(account.capabilities or {}), **info.capabilities}
    account.settings = {**(account.settings or {}), **settings}
    account.status, account.scopes, account.connected_by = status, scopes, user_id
    account.last_checked_at, account.last_error = utcnow(), None
    account.token_expires_at = utcnow() + timedelta(seconds=expires_in) if expires_in else None
    if credentials is not None:
        if account.secret_id:
            await services.vault.update(
                session, business.id, account.secret_id, credentials, expires_at=account.token_expires_at
            )
        else:
            account.secret_id = await services.vault.put(
                session,
                business.id,
                kind="oauth_token",
                label=f"{platform}:{info.external_id}",
                secret=credentials,
                expires_at=account.token_expires_at,
            )
    if status != "practice":  # simulated accounts receive no platform webhooks
        await claim_route(session, business, platform, info.external_id, account.id)
    await append(
        session,
        business_id=business.id,
        type="gtm.account.changed",
        actor=Actor.user(user_id),
        subject=("channel_account", account.id),
        data={"account_id": str(account.id), "platform": platform, "status": status},
    )
    return account


async def connect_practice(
    session: AsyncSession, services: Services, business: Business, *, platform: str, followers: int, user_id: uuid.UUID
) -> ChannelAccount:
    if platform not in PLATFORMS:
        raise ValidationFailed("Unknown platform")
    info = AccountInfo(
        external_id=f"practice-{platform}",
        name=f"Practice {PLATFORMS[platform].label}",
        handle=f"practice_{platform}",
        followers=followers,
        capabilities={"practice": True, "audited": True},
    )
    return await save_account(
        session,
        services,
        business,
        platform=platform,
        info=info,
        credentials=None,
        settings={"provider": "simulator"},
        status="practice",
        scopes=[],
        expires_in=None,
        user_id=user_id,
    )


async def connect_token(
    session: AsyncSession,
    services: Services,
    business: Business,
    *,
    platform: str,
    credentials: dict[str, Any],
    settings: dict[str, Any],
    user_id: uuid.UUID,
) -> ChannelAccount:
    provider = settings.get("provider")
    required = ("profile_key",) if provider == "ayrshare" else TOKEN_PLATFORMS.get(platform)
    if required is None:
        raise ValidationFailed(f"{PLATFORMS.get(platform, platform)} connects with OAuth, not a token")
    missing = [k for k in required if not credentials.get(k)]
    if missing:
        raise ValidationFailed(f"Missing {', '.join(missing)}")
    if platform == "discord" and not str(credentials["webhook_url"]).startswith("https://discord.com/api/webhooks/"):
        raise ValidationFailed("Use a Discord channel webhook URL (https://discord.com/api/webhooks/…)")
    if platform == "whatsapp" and not str(settings.get("phone_number_id", "")).isdigit():
        raise ValidationFailed("Add the WhatsApp phone number ID (digits) from Meta's WhatsApp Manager")
    if platform == "telegram" and not settings.get("chat_id"):
        raise ValidationFailed("Add the channel's chat id or @username, and make the bot an admin of the channel")
    probe = ChannelAccount(
        business_id=business.id, platform=platform, external_id="probe", name="", status="connected", settings=settings
    )
    ctx = AdapterContext(
        http=services.http,
        credentials=credentials,
        account_external_id="probe",
        settings=settings,
        business_id=str(business.id),
    )
    try:
        info = await adapter_for(probe, services.settings).account_info(ctx)
    except AdapterError as exc:
        raise ValidationFailed(f"Couldn't connect: {exc}") from exc
    return await save_account(
        session,
        services,
        business,
        platform=platform,
        info=info,
        credentials=credentials,
        settings={**settings, "handle": info.handle},
        status="connected",
        scopes=[],
        expires_in=None,
        user_id=user_id,
    )


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def authorization_url(platform: str, s: Settings, *, state: str, challenge: str | None) -> str:
    config = oauth_config(platform, s)
    params: dict[str, str] = {
        config.client_id_param: config.client_id or "",
        "redirect_uri": redirect_uri(s, platform),
        "response_type": "code",
        "scope": config.scope_separator.join(config.scopes),
        "state": state,
    }
    if config.pkce and challenge:
        params.update(code_challenge=challenge, code_challenge_method="S256")
    params.update(dict(config.extra))
    return f"{config.authorize_url}?{urlencode(params)}"


async def exchange_code(
    platform: str, s: Settings, http: httpx.AsyncClient, *, code: str, verifier: str | None
) -> dict[str, Any]:
    config = oauth_config(platform, s)
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(s, platform)}
    auth: httpx.BasicAuth | None = None
    if config.basic_auth:
        auth = httpx.BasicAuth(config.client_id or "", config.client_secret or "")
    else:
        data[config.client_id_param] = config.client_id or ""
        data["client_secret"] = config.client_secret or ""
    if config.pkce and verifier:
        data["code_verifier"] = verifier
    response = await http.post(
        config.token_url,
        data=data,
        auth=auth or httpx.USE_CLIENT_DEFAULT,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if response.status_code >= 400:
        raise ValidationFailed(f"The platform refused the connection: {response.text[:200]}")
    tokens: dict[str, Any] = response.json()
    return tokens


async def refresh_tokens(
    platform: str, s: Settings, http: httpx.AsyncClient, credentials: dict[str, Any]
) -> dict[str, Any]:
    config = oauth_config(platform, s)
    if platform in ("facebook", "instagram"):
        return credentials  # long-lived page/user tokens are re-issued on reconnect
    if platform == "threads":
        response = await http.get(
            "https://graph.threads.net/refresh_access_token",
            params={"grant_type": "th_refresh_token", "access_token": credentials["access_token"]},
        )
    else:
        data = {"grant_type": "refresh_token", "refresh_token": credentials.get("refresh_token", "")}
        refresh_auth = (
            httpx.BasicAuth(config.client_id or "", config.client_secret or "") if config.basic_auth else None
        )
        if not config.basic_auth:
            data[config.client_id_param] = config.client_id or ""
            data["client_secret"] = config.client_secret or ""
        response = await http.post(config.token_url, data=data, auth=refresh_auth or httpx.USE_CLIENT_DEFAULT)
    if response.status_code >= 400:
        raise AdapterError(f"refresh failed: {response.text[:200]}", retryable=False)
    fresh = response.json()
    return {**credentials, **fresh, "refresh_token": fresh.get("refresh_token") or credentials.get("refresh_token")}


async def accounts_from_tokens(
    platform: str, s: Settings, http: httpx.AsyncClient, tokens: dict[str, Any]
) -> list[tuple[AccountInfo, dict[str, Any], dict[str, Any]]]:
    """Turn fresh tokens into one or more accounts (a Meta login can grant several Pages)."""
    token = tokens["access_token"]
    if platform in ("facebook", "instagram"):
        graph = f"https://graph.facebook.com/{s.meta_graph_version}"
        long_lived = await http.get(
            f"{graph}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": s.meta_app_id,
                "client_secret": s.meta_app_secret.get_secret_value() if s.meta_app_secret else "",
                "fb_exchange_token": token,
            },
        )
        user_token = long_lived.json().get("access_token", token) if long_lived.status_code == 200 else token
        pages = await http.get(
            f"{graph}/me/accounts",
            params={
                "fields": "id,name,access_token,followers_count,"
                "instagram_business_account{id,username,followers_count}",
                "access_token": user_token,
            },
        )
        out: list[tuple[AccountInfo, dict[str, Any], dict[str, Any]]] = []
        for page in pages.json().get("data", []) if pages.status_code == 200 else []:
            if platform == "facebook":
                out.append(
                    (
                        AccountInfo(
                            external_id=str(page["id"]),
                            name=page.get("name", "Page"),
                            followers=page.get("followers_count"),
                        ),
                        {"page_access_token": page["access_token"]},
                        {},
                    )
                )
            elif page.get("instagram_business_account"):
                ig = page["instagram_business_account"]
                out.append(
                    (
                        AccountInfo(
                            external_id=str(ig["id"]),
                            name=ig.get("username", "Instagram"),
                            handle=ig.get("username"),
                            followers=ig.get("followers_count"),
                        ),
                        {"access_token": user_token, "page_access_token": page["access_token"]},
                        {"page_id": page["id"]},
                    )
                )
        return out
    probe = ChannelAccount(platform=platform, external_id="probe", name="", status="connected", settings={})
    ctx = AdapterContext(http=http, credentials=tokens, account_external_id="probe", settings={}, business_id="")
    info = await adapter_for(probe, s).account_info(ctx)
    settings: dict[str, Any] = {"handle": info.handle}
    return [(info, tokens, settings)]


@periodic("gtm.refresh_tokens", every=timedelta(hours=6))
async def refresh_expiring_tokens(services: Services, now: Any) -> None:
    """Refresh OAuth tokens that expire within three days, so posting never stops by surprise."""
    async with services.db.session() as session:
        business_ids = (await session.execute(select(Business.id).where(Business.status == "active"))).scalars().all()
    for business_id in business_ids:
        async with services.db.tenant(business_id) as session:
            accounts = (
                (
                    await session.execute(
                        select(ChannelAccount).where(
                            ChannelAccount.business_id == business_id,
                            ChannelAccount.status == "connected",
                            ChannelAccount.token_expires_at.is_not(None),
                            ChannelAccount.token_expires_at < utcnow() + timedelta(days=3),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for account in accounts:
                if account.secret_id is None:
                    continue
                try:
                    current = await services.vault.borrow(
                        session, business_id, account.secret_id, purpose="refresh token", actor="gtm.refresh_tokens"
                    )
                    fresh = await refresh_tokens(account.platform, services.settings, services.http, current)
                    expires = int(fresh["expires_in"]) if fresh.get("expires_in") else None
                    account.token_expires_at = utcnow() + timedelta(seconds=expires) if expires else None
                    await services.vault.update(
                        session, business_id, account.secret_id, fresh, expires_at=account.token_expires_at
                    )
                except Exception as exc:  # one bad account must not stop the others
                    account.last_error = f"token refresh failed: {exc}"[:500]
                    if account.token_expires_at and account.token_expires_at < utcnow():
                        account.status = "expired"
