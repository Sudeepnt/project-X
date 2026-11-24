"""Content and channel endpoints: calendar, review, editing, themes, imports, accounts, posts, OAuth."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from projectx.api.deps import BusinessDep, EditorDep, OwnerDep
from projectx.kernel.errors import NotFound, ValidationFailed
from projectx.kernel.time import today_in, utcnow
from projectx.modules.gtm.channels.accounts import (
    accounts_from_tokens,
    authorization_url,
    connect_practice,
    connect_token,
    exchange_code,
    oauth_config,
    pkce_pair,
    save_account,
)
from projectx.modules.gtm.channels.publisher import post_view
from projectx.modules.gtm.config import gtm_settings, update_gtm_settings
from projectx.modules.gtm.content.calendar import upcoming
from projectx.modules.gtm.content.hooks import HOOKS
from projectx.modules.gtm.content.importer import import_items
from projectx.modules.gtm.models import Asset, ChannelAccount, ContentItem, ContentTheme, ContentVariant, Post
from projectx.modules.gtm.platforms import FORMAT_LABELS, PLATFORMS, platform_view
from projectx.services import Services
from projectx.spine.decisions import service as decisions
from projectx.spine.identity.service import consume_email_token, issue_email_token
from projectx.spine.jobs.queue import enqueue
from projectx.spine.ledger.service import Actor, append
from projectx.spine.tenancy.models import Business

router = APIRouter(prefix="/api/v1/b/{business_id}/gtm", tags=["gtm: content & channels"])
public_router = APIRouter(include_in_schema=False)


def media_view(media: list[Any], blobs: Any | None) -> list[dict[str, Any]]:
    """Library files get a short-lived signed URL so the editor can show them; links pass through."""
    out = []
    for m in media or []:
        item = dict(m)
        if blobs is not None and item.get("key") and not item.get("url"):
            item["url"] = blobs.signed_url(item["key"], ttl_s=3600)
        out.append(item)
    return out


def variant_view(v: ContentVariant, blobs: Any | None = None) -> dict[str, Any]:
    return {
        "id": str(v.id),
        "platform": v.platform,
        "platform_label": PLATFORMS[v.platform].label if v.platform in PLATFORMS else v.platform,
        "format": v.format,
        "format_label": FORMAT_LABELS.get(v.format, v.format),
        "title": v.title,
        "text": v.text,
        "thread": v.thread,
        "media": media_view(v.media, blobs),
        "hashtags": v.hashtags,
        "spec": v.spec,
        "status": v.status,
        "scheduled_at": v.scheduled_at.isoformat() if v.scheduled_at else None,
        "account_id": str(v.account_id) if v.account_id else None,
    }


def item_view(i: ContentItem, variants: list[ContentVariant] | None = None, blobs: Any | None = None) -> dict[str, Any]:
    out = {
        "id": str(i.id),
        "campaign_id": str(i.campaign_id) if i.campaign_id else None,
        "theme_id": str(i.theme_id) if i.theme_id else None,
        "pillar": i.pillar,
        "intent": i.intent,
        "hook_type": i.hook_type,
        "title": i.title,
        "core_idea": i.core_idea,
        "body": i.body,
        "cta": i.cta,
        "source_type": i.source_type,
        "source_ref": i.source_ref,
        "status": i.status,
        "checks": i.checks,
        "claims": i.claims,
        "ai_generated": i.ai_generated,
        "edited_by_human": i.edited_by_human,
        "planned_for": i.planned_for.isoformat() if i.planned_for else None,
        "decision_id": str(i.decision_id) if i.decision_id else None,
        "created_at": i.created_at.isoformat(),
    }
    if variants is not None:
        out["variants"] = [variant_view(v, blobs) for v in variants]
    return out


# ------------------------------------------------------------------ content


@router.get("/content")
async def list_content(
    ctx: BusinessDep,
    status_filter: str | None = None,
    campaign_id: uuid.UUID | None = None,
    start: date | None = None,
    end: date | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    stmt = select(ContentItem).where(ContentItem.business_id == ctx.business_id)
    if status_filter:
        stmt = stmt.where(ContentItem.status.in_(status_filter.split(",")))
    if campaign_id:
        stmt = stmt.where(ContentItem.campaign_id == campaign_id)
    if start:
        stmt = stmt.where(ContentItem.planned_for >= datetime.combine(start, datetime.min.time(), UTC))
    if end:
        stmt = stmt.where(ContentItem.planned_for < datetime.combine(end + timedelta(days=1), datetime.min.time(), UTC))
    items = list(
        (
            await ctx.session.execute(stmt.order_by(ContentItem.planned_for.desc().nulls_last()).limit(min(limit, 500)))
        ).scalars()
    )
    variants: dict[uuid.UUID, list[ContentVariant]] = {}
    if items:
        for v in (
            await ctx.session.execute(
                select(ContentVariant).where(ContentVariant.content_id.in_([i.id for i in items]))
            )
        ).scalars():
            variants.setdefault(v.content_id, []).append(v)
    return {"items": [item_view(i, variants.get(i.id, []), ctx.services.blobs) for i in items]}


@router.get("/content/calendar")
async def calendar(ctx: BusinessDep, start: date | None = None, days: int = 14) -> dict[str, Any]:
    first = start or today_in(ctx.business.timezone)
    data = await list_content(ctx, start=first, end=first + timedelta(days=min(days, 62)), limit=500)
    posts = (
        (
            await ctx.session.execute(
                select(Post)
                .where(
                    Post.business_id == ctx.business_id,
                    Post.scheduled_at >= datetime.combine(first, datetime.min.time(), UTC),
                )
                .limit(1000)
            )
        )
        .scalars()
        .all()
    )
    moments = upcoming([ctx.business.country or "US"], first, days=min(days, 62))
    return {
        "start": first.isoformat(),
        "days": days,
        "items": data["items"],
        "posts": [post_view(p) for p in posts],
        "moments": moments,
    }


@router.get("/content/{content_id}")
async def get_content(content_id: uuid.UUID, ctx: BusinessDep) -> dict[str, Any]:
    item = await ctx.session.get(ContentItem, content_id)
    if item is None:
        raise NotFound("Content not found")
    variants = list(
        (await ctx.session.execute(select(ContentVariant).where(ContentVariant.content_id == item.id))).scalars()
    )
    posts = (await ctx.session.execute(select(Post).where(Post.content_id == item.id))).scalars().all()
    return {**item_view(item, variants, ctx.services.blobs), "posts": [post_view(p) for p in posts]}


class ContentIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    idea: str = Field(default="", max_length=2000)
    intent: Literal["value", "ask"] = "value"
    campaign_id: uuid.UUID | None = None
    platforms: list[str] = Field(min_length=1)
    planned_for: datetime | None = None


@router.post("/content", status_code=status.HTTP_201_CREATED)
async def new_content(body: ContentIn, ctx: EditorDep) -> dict[str, Any]:
    from projectx.modules.gtm.platforms import format_for

    formats = {p: f for p in body.platforms if p in PLATFORMS and (f := format_for(p, "text")) is not None}
    if not formats:
        raise ValidationFailed("Pick at least one known platform")
    item = ContentItem(
        business_id=ctx.business_id,
        campaign_id=body.campaign_id,
        intent=body.intent,
        pillar="offer" if body.intent == "ask" else "educate",
        title=body.title,
        core_idea=body.idea,
        source_type="manual",
        status="idea",
        planned_for=body.planned_for or utcnow() + timedelta(hours=2),
        created_by=f"user:{ctx.user.id}",
        source_ref={
            "formats": formats,
            "platforms": list(formats),
            "mix_kind": "text",
            "theme_name": body.title,
            "title": body.title,
            "summary": body.idea,
        },
        ai_generated=True,
    )
    ctx.session.add(item)
    await ctx.session.flush()
    await enqueue(
        ctx.session,
        "gtm.generate_content",
        {"content_id": str(item.id)},
        business_id=ctx.business_id,
        idempotency_key=f"gen:{item.id}",
    )
    return item_view(item, [])


class ContentPatch(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    body: str | None = Field(default=None, max_length=8000)
    cta: str | None = Field(default=None, max_length=300)
    planned_for: datetime | None = None


@router.patch("/content/{content_id}")
async def edit_content(content_id: uuid.UUID, body: ContentPatch, ctx: EditorDep) -> dict[str, Any]:
    item = await ctx.session.get(ContentItem, content_id)
    if item is None:
        raise NotFound("Content not found")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(item, key, value)
    item.edited_by_human = True
    if body.planned_for:
        for v in (
            await ctx.session.execute(select(ContentVariant).where(ContentVariant.content_id == item.id))
        ).scalars():
            v.scheduled_at = body.planned_for
    return item_view(item)


class MediaIn(BaseModel):
    """Either a file from this business's asset library (`key`) or a public https URL."""

    key: str | None = Field(default=None, max_length=300)
    url: str | None = Field(default=None, max_length=2000)
    kind: Literal["image", "video", "document"] = "image"
    alt_text: str | None = Field(default=None, max_length=500)


class VariantPatch(BaseModel):
    text: str | None = Field(default=None, max_length=10_000)
    title: str | None = Field(default=None, max_length=300)
    thread: list[str] | None = Field(default=None, max_length=25)
    media: list[MediaIn] | None = Field(default=None, max_length=10)


async def _validated_media(ctx: Any, media: list[MediaIn]) -> list[dict[str, Any]]:
    """Never let a post point at another business's files, or at non-https URLs."""
    out: list[dict[str, Any]] = []
    for m in media:
        if m.key:
            asset = await ctx.session.scalar(
                select(Asset).where(Asset.business_id == ctx.business_id, Asset.storage_key == m.key)
            )
            if asset is None:
                raise ValidationFailed("That file isn't in your asset library")
            out.append({"key": asset.storage_key, "kind": asset.kind, "alt_text": m.alt_text or asset.alt_text})
        elif m.url and m.url.startswith("https://"):
            out.append({"url": m.url, "kind": m.kind, "alt_text": m.alt_text})
        else:
            raise ValidationFailed("Media must be a library file or an https link")
    return out


@router.patch("/variants/{variant_id}")
async def edit_variant(variant_id: uuid.UUID, body: VariantPatch, ctx: EditorDep) -> dict[str, Any]:
    from projectx.modules.gtm.content.checks import spec_check

    variant = await ctx.session.get(ContentVariant, variant_id)
    if variant is None:
        raise NotFound("Variant not found")
    if variant.status in ("published", "publishing"):
        raise ValidationFailed("This version is already published")
    changes = body.model_dump(exclude_unset=True, exclude={"media"})
    for key, value in changes.items():
        setattr(variant, key, value)
    if body.media is not None:
        variant.media = await _validated_media(ctx, body.media)
    item = await ctx.session.get(ContentItem, variant.content_id)
    if item is not None:
        item.edited_by_human = True
    specs = spec_check(variant, practice=ctx.business.practice_mode)
    variant.spec = {**(variant.spec or {}), "specs": specs.as_dict()}
    if variant.status in ("blocked", "needs_review") and specs.passed:
        variant.status = "ready"
    return variant_view(variant, ctx.services.blobs)


@router.post("/content/{content_id}/regenerate")
async def regenerate(content_id: uuid.UUID, ctx: EditorDep) -> dict[str, Any]:
    item = await ctx.session.get(ContentItem, content_id)
    if item is None:
        raise NotFound("Content not found")
    if item.status in ("scheduled", "published"):
        raise ValidationFailed("Scheduled or published content can't be regenerated")
    if item.decision_id:
        await decisions.cancel(ctx.session, ctx.business_id, item.decision_id, reason="regenerated")
    for v in (await ctx.session.execute(select(ContentVariant).where(ContentVariant.content_id == item.id))).scalars():
        await ctx.session.delete(v)
    item.status, item.decision_id = "idea", None
    await enqueue(
        ctx.session,
        "gtm.generate_content",
        {"content_id": str(item.id)},
        business_id=ctx.business_id,
        idempotency_key=f"gen:{item.id}:{uuid.uuid4().hex[:8]}",
    )
    return item_view(item, [])


@router.post("/content/{content_id}/archive")
async def archive(content_id: uuid.UUID, ctx: EditorDep) -> dict[str, Any]:
    item = await ctx.session.get(ContentItem, content_id)
    if item is None:
        raise NotFound("Content not found")
    if item.decision_id:
        await decisions.cancel(ctx.session, ctx.business_id, item.decision_id, reason="archived")
    item.status = "archived"
    await append(
        ctx.session,
        business_id=ctx.business_id,
        type="gtm.content.changed",
        actor=Actor.user(ctx.user.id),
        subject=("content", item.id),
        data={"content_id": str(item.id), "status": "archived"},
    )
    return item_view(item)


class PlanIn(BaseModel):
    campaign_id: uuid.UUID
    days: int = Field(default=7, ge=1, le=31)


@router.post("/content/plan", status_code=status.HTTP_202_ACCEPTED)
async def plan_now(body: PlanIn, ctx: EditorDep) -> dict[str, Any]:
    job_id = await enqueue(
        ctx.session,
        "gtm.plan_content",
        {"campaign_id": str(body.campaign_id), "days": body.days},
        business_id=ctx.business_id,
    )
    return {"job_id": str(job_id) if job_id else None}


class StoryIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(default="", max_length=4000)
    url: str | None = Field(default=None, max_length=2000)
    image_url: str | None = Field(default=None, max_length=2000)
    external_id: str | None = Field(default=None, max_length=300)
    published_at: str | None = None


class ImportIn(BaseModel):
    items: list[StoryIn] = Field(min_length=1, max_length=100)
    campaign_id: uuid.UUID | None = None


@router.post("/content/import", status_code=status.HTTP_202_ACCEPTED)
async def import_content(body: ImportIn, ctx: EditorDep) -> dict[str, Any]:
    created = await import_items(
        ctx.session, ctx.business, [i.model_dump() for i in body.items], campaign_id=body.campaign_id
    )
    return {"created": len(created), "content_ids": [str(c) for c in created]}


class RepurposeIn(BaseModel):
    title: str = Field(default="", max_length=200)
    text: str = Field(min_length=1, max_length=60_000)
    source_url: str | None = Field(default=None, max_length=2000, pattern=r"^https?://")
    count: int = Field(default=5, ge=2, le=10)
    platforms: list[str] | None = Field(default=None, max_length=14)
    campaign_id: uuid.UUID | None = None


@router.post("/content/repurpose", status_code=status.HTTP_202_ACCEPTED)
async def repurpose_content(body: RepurposeIn, ctx: EditorDep) -> dict[str, Any]:
    """Turn one long piece into several posts. They arrive for review in a minute or two."""
    from projectx.modules.gtm.content.repurpose import queue_repurpose

    job_id = await queue_repurpose(
        ctx.session,
        ctx.business,
        title=body.title,
        text=body.text,
        source_url=body.source_url,
        count=body.count,
        platforms=body.platforms,
        campaign_id=body.campaign_id,
    )
    return {"job_id": str(job_id) if job_id else None, "count": body.count}


class FeedsIn(BaseModel):
    feeds: list[str] = Field(max_length=5)


@router.put("/content/feeds")
async def set_feeds(body: FeedsIn, ctx: EditorDep) -> dict[str, Any]:
    for url in body.feeds:
        if not url.startswith(("http://", "https://")):
            raise ValidationFailed("Feeds are http(s) URLs")
    update_gtm_settings(ctx.business, {"feeds": body.feeds})
    await enqueue(ctx.session, "gtm.import_feeds", business_id=ctx.business_id)
    return {"feeds": body.feeds}


@router.get("/content-meta")
async def content_meta(ctx: BusinessDep) -> dict[str, Any]:
    return {
        "hooks": [{"key": h.key, "label": h.label, "pattern": h.pattern, "example": h.example} for h in HOOKS.values()],
        "formats": FORMAT_LABELS,
        "feeds": gtm_settings(ctx.business).get("feeds", []),
        "moments": upcoming([ctx.business.country or "US"], today_in(ctx.business.timezone), days=60),
    }


# ------------------------------------------------------------------ themes


class ThemeIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    pillar: Literal["educate", "entertain", "inspire", "behind_the_scenes", "proof", "offer"]
    description: str = Field(default="", max_length=1000)
    weight: float = Field(default=1.0, ge=0, le=5)
    active: bool = True


def theme_view(t: ContentTheme) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "name": t.name,
        "pillar": t.pillar,
        "description": t.description,
        "weight": t.weight,
        "active": t.active,
    }


@router.get("/themes")
async def themes(ctx: BusinessDep) -> list[dict[str, Any]]:
    rows = (
        (
            await ctx.session.execute(
                select(ContentTheme)
                .where(ContentTheme.business_id == ctx.business_id)
                .order_by(ContentTheme.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [theme_view(t) for t in rows]


@router.post("/themes", status_code=status.HTTP_201_CREATED)
async def add_theme(body: ThemeIn, ctx: EditorDep) -> dict[str, Any]:
    theme = ContentTheme(business_id=ctx.business_id, **body.model_dump())
    ctx.session.add(theme)
    await ctx.session.flush()
    return theme_view(theme)


@router.put("/themes/{theme_id}")
async def update_theme(theme_id: uuid.UUID, body: ThemeIn, ctx: EditorDep) -> dict[str, Any]:
    theme = await ctx.session.get(ContentTheme, theme_id)
    if theme is None:
        raise NotFound("Theme not found")
    for key, value in body.model_dump().items():
        setattr(theme, key, value)
    return theme_view(theme)


# ------------------------------------------------------------------ channels


def account_view(a: ChannelAccount) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "platform": a.platform,
        "external_id": a.external_id,
        "name": a.name,
        "handle": a.handle,
        "avatar_url": a.avatar_url,
        "status": a.status,
        "scopes": a.scopes,
        "followers": a.followers,
        "capabilities": a.capabilities,
        "settings": dict((a.settings or {}).items()),
        "token_expires_at": a.token_expires_at.isoformat() if a.token_expires_at else None,
        "last_error": a.last_error,
        "created_at": a.created_at.isoformat(),
    }


@router.get("/channels")
async def channels(ctx: BusinessDep) -> dict[str, Any]:
    accounts = (
        (
            await ctx.session.execute(
                select(ChannelAccount)
                .where(ChannelAccount.business_id == ctx.business_id)
                .order_by(ChannelAccount.created_at)
            )
        )
        .scalars()
        .all()
    )
    platforms = []
    for p in PLATFORMS.values():
        configured = True
        if p.oauth:
            try:
                oauth_config(p.key, ctx.services.settings)
            except Exception:
                configured = False
        platforms.append(
            {
                **platform_view(p),
                "app_configured": configured,
                "accounts": [account_view(a) for a in accounts if a.platform == p.key],
            }
        )
    return {"platforms": platforms, "practice_mode": ctx.business.practice_mode}


class PracticeIn(BaseModel):
    platform: str
    followers: int = Field(default=1000, ge=0, le=10_000_000)


@router.post("/channels/practice", status_code=status.HTTP_201_CREATED)
async def add_practice(body: PracticeIn, ctx: EditorDep) -> dict[str, Any]:
    account = await connect_practice(
        ctx.session, ctx.services, ctx.business, platform=body.platform, followers=body.followers, user_id=ctx.user.id
    )
    return account_view(account)


class TokenIn(BaseModel):
    platform: str
    credentials: dict[str, str]
    settings: dict[str, Any] = Field(default_factory=dict)


@router.post("/channels/token", status_code=status.HTTP_201_CREATED)
async def add_token_account(body: TokenIn, ctx: OwnerDep) -> dict[str, Any]:
    account = await connect_token(
        ctx.session,
        ctx.services,
        ctx.business,
        platform=body.platform,
        credentials=body.credentials,
        settings=body.settings,
        user_id=ctx.user.id,
    )
    return account_view(account)


@router.get("/channels/oauth/{platform}/start")
async def oauth_start(platform: str, ctx: OwnerDep) -> dict[str, str]:
    if platform not in PLATFORMS or not PLATFORMS[platform].oauth:
        raise NotFound("Unknown platform")
    verifier, challenge = pkce_pair()
    state = await issue_email_token(
        ctx.session,
        user_id=ctx.user.id,
        purpose="oauth",
        ttl=timedelta(minutes=15),
        payload={"business_id": str(ctx.business_id), "platform": platform, "verifier": verifier},
    )
    return {"url": authorization_url(platform, ctx.services.settings, state=state, challenge=challenge)}


@public_router.get("/oauth/{platform}/callback")
async def oauth_callback(platform: str, request: Request) -> RedirectResponse:
    services: Services = request.app.state.services
    web = services.settings.web_url
    code, state = request.query_params.get("code"), request.query_params.get("state")
    if request.query_params.get("error") or not code or not state:
        return RedirectResponse(f"{web}/channels?error=denied", status_code=303)
    async with services.db.session() as session:
        try:
            token_row = await consume_email_token(session, state, "oauth")
        except ValidationFailed:
            return RedirectResponse(f"{web}/channels?error=expired", status_code=303)
        payload, user_id = token_row.payload, token_row.user_id
    if payload.get("platform") != platform or user_id is None:
        return RedirectResponse(f"{web}/channels?error=mismatch", status_code=303)
    business_id = uuid.UUID(payload["business_id"])
    try:
        tokens = await exchange_code(
            platform, services.settings, services.http, code=code, verifier=payload.get("verifier")
        )
        found = await accounts_from_tokens(platform, services.settings, services.http, tokens)
    except Exception:
        return RedirectResponse(f"{web}/channels?error=connect_failed&platform={platform}", status_code=303)
    async with services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        if business is None:
            return RedirectResponse(f"{web}/channels?error=unknown_business", status_code=303)
        scopes = str(tokens.get("scope", "")).replace(",", " ").split()
        for info, credentials, settings in found:
            await save_account(
                session,
                services,
                business,
                platform=platform,
                info=info,
                credentials={**credentials, "refresh_token": tokens.get("refresh_token")},
                settings=settings,
                status="connected",
                scopes=scopes,
                expires_in=int(tokens["expires_in"]) if tokens.get("expires_in") else None,
                user_id=user_id,
            )
    return RedirectResponse(f"{web}/channels?connected={platform}&count={len(found)}", status_code=303)


class AccountPatch(BaseModel):
    settings: dict[str, Any]


@router.patch("/channels/accounts/{account_id}")
async def update_account(account_id: uuid.UUID, body: AccountPatch, ctx: EditorDep) -> dict[str, Any]:
    account = await ctx.session.get(ChannelAccount, account_id)
    if account is None:
        raise NotFound("Account not found")
    allowed = {
        "board_id",
        "link",
        "privacy",
        "privacy_level",
        "organization_urn",
        "posting_hours",
        "paused",
        "guild_id",
        "username",
    }
    unknown = set(body.settings) - allowed
    if unknown:
        raise ValidationFailed(f"Unknown settings: {sorted(unknown)}")
    account.settings = {**(account.settings or {}), **body.settings}
    return account_view(account)


@router.delete("/channels/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect(account_id: uuid.UUID, ctx: OwnerDep) -> None:
    account = await ctx.session.get(ChannelAccount, account_id)
    if account is None:
        raise NotFound("Account not found")
    if account.secret_id:
        await ctx.services.vault.delete(ctx.session, ctx.business_id, account.secret_id)
        await append(
            ctx.session,
            business_id=ctx.business_id,
            type="spine.credential.changed",
            actor=Actor.user(ctx.user.id),
            data={"secret_id": str(account.secret_id), "kind": "oauth_token", "change": "deleted"},
        )
    account.secret_id, account.status = None, "revoked"
    await append(
        ctx.session,
        business_id=ctx.business_id,
        type="gtm.account.changed",
        actor=Actor.user(ctx.user.id),
        subject=("channel_account", account.id),
        data={"account_id": str(account.id), "platform": account.platform, "status": "revoked"},
    )


@router.get("/posts")
async def posts(
    ctx: BusinessDep, status_filter: str | None = None, platform: str | None = None, limit: int = 100
) -> dict[str, Any]:
    stmt = select(Post).where(Post.business_id == ctx.business_id)
    if status_filter:
        stmt = stmt.where(Post.status.in_(status_filter.split(",")))
    if platform:
        stmt = stmt.where(Post.platform == platform)
    rows = (
        (await ctx.session.execute(stmt.order_by(Post.scheduled_at.desc().nulls_last()).limit(min(limit, 500))))
        .scalars()
        .all()
    )
    return {"items": [post_view(p) for p in rows]}


@router.post("/posts/{post_id}/retry")
async def retry_post(post_id: uuid.UUID, ctx: EditorDep) -> dict[str, Any]:
    post = await ctx.session.get(Post, post_id)
    if post is None:
        raise NotFound("Post not found")
    if post.status != "failed":
        raise ValidationFailed("Only failed posts can be retried")
    post.status, post.last_error = "queued", None
    await enqueue(
        ctx.session,
        "gtm.publish",
        {"post_id": str(post.id)},
        business_id=ctx.business_id,
        idempotency_key=f"publish:{post.id}:{uuid.uuid4().hex[:6]}",
    )
    return post_view(post)


@router.post("/channels/sync-metrics", status_code=status.HTTP_202_ACCEPTED)
async def sync_now(ctx: EditorDep) -> dict[str, Any]:
    job_id = await enqueue(ctx.session, "gtm.sync_metrics", business_id=ctx.business_id)
    return {"job_id": str(job_id) if job_id else None}
