"""Ads endpoints: accounts and caps, campaigns, budgets, pause/resume, sync."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any, Literal

from fastapi import APIRouter, status
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import select

from projectx.api.deps import AdminDep, BusinessDep, EditorDep, OwnerDep
from projectx.kernel.errors import NotFound
from projectx.kernel.time import today_in
from projectx.modules.gtm.ads import service as ads
from projectx.modules.gtm.models import AdAccount, AdCampaign, AdMetricDaily, ContentItem, Post
from projectx.spine.billing.plans import require_feature
from projectx.spine.jobs.queue import enqueue
from projectx.spine.ledger.service import Actor

router = APIRouter(prefix="/api/v1/b/{business_id}/gtm/ads", tags=["gtm: ads"])

Platform = Literal["meta", "google"]


@router.get("")
async def overview(ctx: BusinessDep, days: int = 30) -> dict[str, Any]:
    today = today_in(ctx.business.timezone)
    accounts = (await ctx.session.execute(select(AdAccount).order_by(AdAccount.created_at))).scalars().all()
    campaigns = (await ctx.session.execute(select(AdCampaign).order_by(AdCampaign.created_at.desc()))).scalars().all()
    since = today - timedelta(days=max(1, min(days, 365)) - 1)
    metrics = (await ctx.session.execute(select(AdMetricDaily).where(AdMetricDaily.day >= since))).scalars().all()
    by_campaign: dict[uuid.UUID, list[AdMetricDaily]] = {}
    for m in metrics:
        by_campaign.setdefault(m.ad_campaign_id, []).append(m)
    return {
        "practice_mode": ctx.business.practice_mode,
        "accounts": [
            ads.account_view(a, spent_this_month=await ads.month_spend(ctx.session, a.id, today)) for a in accounts
        ],
        "campaigns": [ads.campaign_view(c, by_campaign.get(c.id, [])) for c in campaigns],
    }


@router.get("/candidates")
async def candidates(ctx: BusinessDep, limit: int = 10) -> dict[str, Any]:
    """Posts that already worked organically: the only things worth putting money behind."""
    rows = (
        await ctx.session.execute(
            select(Post, ContentItem)
            .join(ContentItem, ContentItem.id == Post.content_id)
            .where(Post.status == "published", Post.score.is_not(None))
            .order_by(Post.score.desc())
            .limit(max(1, min(limit, 50)))
        )
    ).all()
    seen: set[uuid.UUID] = set()
    out = []
    for post, item in rows:
        if item.id in seen:
            continue
        seen.add(item.id)
        out.append(
            {
                "content_id": str(item.id),
                "title": item.title,
                "platform": post.platform,
                "score": post.score,
                "metrics": post.metrics,
                "url": post.url,
            }
        )
    return {"items": out}


class PracticeAccountIn(BaseModel):
    platform: Platform
    monthly_cap_minor: int = Field(ge=0, le=10**12)


@router.post("/accounts/practice", status_code=status.HTTP_201_CREATED)
async def add_practice_account(body: PracticeAccountIn, ctx: OwnerDep) -> dict[str, Any]:
    require_feature(ctx.business, "ads")
    account = await ads.connect_practice(
        ctx.session, ctx.business, platform=body.platform, monthly_cap_minor=body.monthly_cap_minor
    )
    return ads.account_view(account)


class TokenAccountIn(BaseModel):
    platform: Platform
    external_id: str = Field(min_length=3, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    credentials: dict[str, str]
    settings: dict[str, str] = Field(default_factory=dict)
    monthly_cap_minor: int = Field(ge=0, le=10**12)


@router.post("/accounts", status_code=status.HTTP_201_CREATED)
async def add_account(body: TokenAccountIn, ctx: OwnerDep) -> dict[str, Any]:
    require_feature(ctx.business, "ads")
    account = await ads.connect_token(
        ctx.session,
        ctx.services,
        ctx.business,
        platform=body.platform,
        external_id=body.external_id,
        name=body.name,
        credentials=body.credentials,
        settings=body.settings,
        monthly_cap_minor=body.monthly_cap_minor,
    )
    return ads.account_view(account)


class CapIn(BaseModel):
    monthly_cap_minor: int = Field(ge=0, le=10**12)


@router.put("/accounts/{account_id}/cap")
async def set_cap(account_id: uuid.UUID, body: CapIn, ctx: OwnerDep) -> dict[str, Any]:
    """Only the owner sets the ceiling. Lowering it below what's committed pauses campaigns on the next check."""
    account = await ctx.session.get(AdAccount, account_id)
    if account is None:
        raise NotFound("Ad account not found")
    account.monthly_cap_minor = body.monthly_cap_minor
    await ads.mirror_cap(ctx.session, ctx.services, account)
    await enqueue(ctx.session, "gtm.ads_daily", business_id=ctx.business_id)
    return ads.account_view(
        account, spent_this_month=await ads.month_spend(ctx.session, account.id, today_in(ctx.business.timezone))
    )


class CampaignIn(BaseModel):
    ad_account_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    objective: Literal["leads", "traffic", "sales"] = "leads"
    daily_budget_minor: int = Field(gt=0, le=10**10)
    audience_kind: Literal["retargeting", "lookalike", "cold"] = "retargeting"
    special_category: Literal["housing", "employment", "credit", "politics"] | None = None
    content_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5)
    landing_url: HttpUrl | None = None
    target_cpl_minor: int | None = Field(default=None, gt=0)
    campaign_id: uuid.UUID | None = None
    external_campaign_id: str | None = Field(default=None, max_length=40)
    ends_on: date | None = None


@router.post("/campaigns", status_code=status.HTTP_201_CREATED)
async def create_campaign(body: CampaignIn, ctx: EditorDep) -> dict[str, Any]:
    require_feature(ctx.business, "ads")
    account = await ctx.session.get(AdAccount, body.ad_account_id)
    if account is None:
        raise NotFound("Ad account not found")
    ad, outcome = await ads.propose_campaign(
        ctx.session,
        ctx.business,
        account=account,
        name=body.name,
        objective=body.objective,
        daily_budget_minor=body.daily_budget_minor,
        audience_kind=body.audience_kind,
        special_category=body.special_category,
        content_ids=body.content_ids,
        landing_url=str(body.landing_url) if body.landing_url else None,
        target_cpl_minor=body.target_cpl_minor,
        campaign_id=body.campaign_id,
        external_campaign_id=body.external_campaign_id,
        ends_on=body.ends_on,
        requested_by=f"user:{ctx.user.id}",
    )
    return {
        "campaign": ads.campaign_view(ad, []),
        "status": outcome.status,
        "decision_id": str(outcome.decision_id) if outcome.decision_id else None,
        "reasons": [r.message for r in outcome.verdict.reasons],
    }


async def _get(ctx: BusinessDep, ad_id: uuid.UUID) -> AdCampaign:
    ad = await ctx.session.get(AdCampaign, ad_id)
    if ad is None:
        raise NotFound("Ad campaign not found")
    return ad


class BudgetIn(BaseModel):
    daily_budget_minor: int = Field(gt=0, le=10**10)


@router.post("/campaigns/{ad_id}/budget")
async def change_budget(ad_id: uuid.UUID, body: BudgetIn, ctx: EditorDep) -> dict[str, Any]:
    require_feature(ctx.business, "ads")
    ad = await _get(ctx, ad_id)
    outcome = await ads.propose_budget(
        ctx.session,
        ctx.business,
        ad,
        daily_budget_minor=body.daily_budget_minor,
        requested_by=f"user:{ctx.user.id}",
        services=ctx.services,
    )
    if outcome is None:
        return {"status": "applied", "campaign": ads.campaign_view(ad, [])}
    return {
        "status": outcome.status,
        "decision_id": str(outcome.decision_id) if outcome.decision_id else None,
        "reasons": [r.message for r in outcome.verdict.reasons],
    }


@router.post("/campaigns/{ad_id}/pause")
async def pause(ad_id: uuid.UUID, ctx: EditorDep) -> dict[str, Any]:
    ad = await _get(ctx, ad_id)
    result = await ads.pause_in(ctx.session, ctx.services, ad, "Paused by founder", Actor.user(ctx.user.id))
    return {**result, "campaign": ads.campaign_view(ad, [])}


@router.post("/campaigns/{ad_id}/resume")
async def resume(ad_id: uuid.UUID, ctx: AdminDep) -> dict[str, Any]:
    require_feature(ctx.business, "ads")
    ad = await _get(ctx, ad_id)
    await ads.resume(ctx.session, ctx.services, ctx.business, ad, actor=Actor.user(ctx.user.id))
    return {"campaign": ads.campaign_view(ad, [])}


@router.post("/sync", status_code=status.HTTP_202_ACCEPTED)
async def sync(ctx: EditorDep) -> dict[str, Any]:
    job_id = await enqueue(ctx.session, "gtm.ads_daily", business_id=ctx.business_id)
    return {"job_id": str(job_id) if job_id else None}
