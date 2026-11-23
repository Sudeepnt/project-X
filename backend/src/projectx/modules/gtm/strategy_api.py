"""Strategy endpoints: brand book, customer profiles, diagnosis, goals, restrictions, campaigns."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from projectx.api.deps import AdminDep, BusinessDep, EditorDep
from projectx.kernel.errors import Conflict, NotFound
from projectx.modules.gtm.brand.check import brand_check, claims_check, find_claims
from projectx.modules.gtm.brand.service import approve_book, audience_view, book_view, current_book, latest_draft
from projectx.modules.gtm.campaigns.library import CATEGORIES, get_playbook, playbooks
from projectx.modules.gtm.campaigns.selection import SelectionContext, recommend
from projectx.modules.gtm.campaigns.service import campaign_view, create_campaign, set_status, start_campaign
from projectx.modules.gtm.goals import restrictions as restrictions_svc
from projectx.modules.gtm.goals.diagnosis import diagnose
from projectx.modules.gtm.goals.forecast import forecast
from projectx.modules.gtm.goals.metrics import DEFAULT_METRIC, METRICS
from projectx.modules.gtm.goals.service import (
    activate_goal,
    build_forecast_input,
    create_goal,
    end_goal,
    goal_view,
    pace_goal,
)
from projectx.modules.gtm.models import Audience, BrandBook, Campaign, Goal, Restriction
from projectx.spine.billing.plans import require_feature
from projectx.spine.jobs.queue import enqueue
from projectx.spine.ledger.models import LedgerEvent
from projectx.spine.ledger.service import Actor

router = APIRouter(prefix="/api/v1/b/{business_id}/gtm", tags=["gtm: strategy"])

GoalType = Literal["awareness", "demand", "sales", "partnerships", "retention", "reputation"]


# ------------------------------------------------------------------ brand book


class DraftIn(BaseModel):
    website: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=4000)


class BookPatch(BaseModel):
    summary: str | None = Field(default=None, max_length=4000)
    voice: dict[str, Any] | None = None
    offer: dict[str, Any] | None = None
    audience_summary: str | None = Field(default=None, max_length=4000)
    positioning: dict[str, Any] | None = None
    visual: dict[str, Any] | None = None
    do_list: list[str] | None = None
    dont_list: list[str] | None = None
    banned_words: list[str] | None = None
    proof_points: list[dict[str, Any]] | None = None
    hashtags: list[str] | None = None


@router.get("/brand-book")
async def brand_book(ctx: BusinessDep) -> dict[str, Any]:
    current = await current_book(ctx.session, ctx.business_id)
    draft = await latest_draft(ctx.session, ctx.business_id)
    return {"current": book_view(current) if current else None, "draft": book_view(draft) if draft else None}


@router.post("/brand-book/draft", status_code=status.HTTP_202_ACCEPTED)
async def draft_brand_book(body: DraftIn, ctx: EditorDep) -> dict[str, Any]:
    job_id = await enqueue(
        ctx.session, "gtm.draft_brand_book", {"website": body.website, "notes": body.notes}, business_id=ctx.business_id
    )
    return {"job_id": str(job_id) if job_id else None}


@router.post("/brand-book/edit", status_code=status.HTTP_201_CREATED)
async def start_edit(ctx: EditorDep) -> dict[str, Any]:
    """Start a new draft from the approved version (approved versions are never edited in place)."""
    existing = await latest_draft(ctx.session, ctx.business_id)
    if existing is not None:
        return book_view(existing)
    current = await current_book(ctx.session, ctx.business_id)
    top = await ctx.session.scalar(select(func.max(BrandBook.version)).where(BrandBook.business_id == ctx.business_id))
    fields = book_view(current) if current else {}
    book = BrandBook(
        business_id=ctx.business_id,
        version=int(top or 0) + 1,
        status="draft",
        summary=fields.get("summary", ""),
        voice=fields.get("voice", {}),
        offer=fields.get("offer", {}),
        audience_summary=fields.get("audience_summary", ""),
        positioning=fields.get("positioning", {}),
        visual=fields.get("visual", {}),
        do_list=fields.get("do_list", []),
        dont_list=fields.get("dont_list", []),
        banned_words=fields.get("banned_words", []),
        proof_points=fields.get("proof_points", []),
        hashtags=fields.get("hashtags", []),
        sources={},
        created_by=f"user:{ctx.user.id}",
    )
    ctx.session.add(book)
    await ctx.session.flush()
    return book_view(book)


@router.patch("/brand-book/{book_id}")
async def edit_brand_book(book_id: uuid.UUID, body: BookPatch, ctx: EditorDep) -> dict[str, Any]:
    book = await ctx.session.get(BrandBook, book_id)
    if book is None:
        raise NotFound("Brand book not found")
    if book.status != "draft":
        raise Conflict("Approved versions can't be edited; start a new draft")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(book, key, value)
    return book_view(book)


@router.post("/brand-book/{book_id}/approve")
async def approve_brand_book(book_id: uuid.UUID, ctx: AdminDep) -> dict[str, Any]:
    book = await ctx.session.get(BrandBook, book_id)
    if book is None:
        raise NotFound("Brand book not found")
    await approve_book(ctx.session, ctx.business, book, user_id=ctx.user.id)
    return book_view(book)


@router.get("/brand-book/versions")
async def brand_book_versions(ctx: BusinessDep) -> list[dict[str, Any]]:
    rows = (
        (
            await ctx.session.execute(
                select(BrandBook).where(BrandBook.business_id == ctx.business_id).order_by(BrandBook.version.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(b.id),
            "version": b.version,
            "status": b.status,
            "created_at": b.created_at.isoformat(),
            "approved_at": b.approved_at.isoformat() if b.approved_at else None,
        }
        for b in rows
    ]


class CheckIn(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)


@router.post("/brand-book/check")
async def check_text(body: CheckIn, ctx: BusinessDep) -> dict[str, Any]:
    book = await current_book(ctx.session, ctx.business_id)
    rules = await restrictions_svc.active(ctx.session, ctx.business_id)
    brand = brand_check(body.text, book, extra_banned=restrictions_svc.banned_phrases(rules))
    evidence = [f"{p.get('claim', '')} {p.get('evidence', '')}" for p in (book.proof_points if book else [])]
    claims = claims_check(find_claims(body.text), evidence=evidence, strict=restrictions_svc.claims_strict(rules))
    return {"brand": brand.as_dict(), "claims": claims.as_dict()}


# ------------------------------------------------------------------ audiences


class AudienceIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)
    segment: Literal["b2c", "b2b"] = "b2c"
    is_primary: bool = False
    demographics: dict[str, Any] = Field(default_factory=dict)
    firmographics: dict[str, Any] = Field(default_factory=dict)
    pains: list[str] = Field(default_factory=list)
    desires: list[str] = Field(default_factory=list)
    objections: list[str] = Field(default_factory=list)
    buying_triggers: list[str] = Field(default_factory=list)
    platform_affinity: dict[str, float] = Field(default_factory=dict)
    locations: list[str] = Field(default_factory=list)


@router.get("/audiences")
async def audiences(ctx: BusinessDep) -> list[dict[str, Any]]:
    rows = (
        (
            await ctx.session.execute(
                select(Audience)
                .where(Audience.business_id == ctx.business_id)
                .order_by(Audience.is_primary.desc(), Audience.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [audience_view(a) for a in rows]


async def _demote_primary(ctx: EditorDep, keep: uuid.UUID | None) -> None:
    for a in (
        await ctx.session.execute(
            select(Audience).where(Audience.business_id == ctx.business_id, Audience.is_primary.is_(True))
        )
    ).scalars():
        if a.id != keep:
            a.is_primary = False


@router.post("/audiences", status_code=status.HTTP_201_CREATED)
async def add_audience(body: AudienceIn, ctx: EditorDep) -> dict[str, Any]:
    audience = Audience(business_id=ctx.business_id, **body.model_dump())
    ctx.session.add(audience)
    await ctx.session.flush()
    if body.is_primary:
        await _demote_primary(ctx, audience.id)
    return audience_view(audience)


@router.put("/audiences/{audience_id}")
async def update_audience(audience_id: uuid.UUID, body: AudienceIn, ctx: EditorDep) -> dict[str, Any]:
    audience = await ctx.session.get(Audience, audience_id)
    if audience is None:
        raise NotFound("Profile not found")
    for key, value in body.model_dump().items():
        setattr(audience, key, value)
    if body.is_primary:
        await _demote_primary(ctx, audience.id)
    return audience_view(audience)


@router.delete("/audiences/{audience_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_audience(audience_id: uuid.UUID, ctx: EditorDep) -> None:
    audience = await ctx.session.get(Audience, audience_id)
    if audience is None:
        raise NotFound("Profile not found")
    await ctx.session.delete(audience)


# ------------------------------------------------------------------ diagnosis and goals


@router.get("/diagnosis")
async def diagnosis(ctx: BusinessDep, launching: bool = False) -> dict[str, Any]:
    return await diagnose(ctx.session, ctx.business, launching=launching)


class ForecastIn(BaseModel):
    type: GoalType
    metric: str | None = None
    target: float = Field(gt=0)
    deadline: date
    budget_minor: int = Field(default=0, ge=0)
    channels: list[str] | None = None
    paid_channels: list[Literal["meta", "google", "linkedin"]] | None = None


class GoalIn(ForecastIn):
    title: str | None = Field(default=None, max_length=200)
    growth_approach: Literal["content_community", "sales_referrals", "partner_led", "product_led"] = "content_community"
    approval_mode: Literal["approve_all", "approve_new", "auto"] = "approve_all"
    guardrail: dict[str, Any] | None = None
    stop_rules: list[dict[str, Any]] = Field(default_factory=list)
    ai_credit_budget_cents: int | None = Field(default=None, ge=0)
    restrictions: list[dict[str, Any]] = Field(default_factory=list)
    activate: bool = False
    replace_active: bool = False


@router.get("/goals/metrics")
async def goal_metrics(ctx: BusinessDep) -> dict[str, Any]:
    return {
        "metrics": [
            {
                "key": m.key,
                "label": m.label,
                "goal_type": m.goal_type,
                "unit": m.unit,
                "stage": m.stage,
                "higher_is_better": m.higher_is_better,
            }
            for m in METRICS.values()
        ],
        "defaults": DEFAULT_METRIC,
        "restriction_kinds": restrictions_svc.KINDS,
    }


@router.post("/goals/forecast")
async def forecast_goal(body: ForecastIn, ctx: BusinessDep) -> dict[str, Any]:
    metric = body.metric or DEFAULT_METRIC[body.type]
    inp = await build_forecast_input(
        ctx.session,
        ctx.business,
        goal_type=body.type,
        metric=metric,
        target=body.target,
        deadline=body.deadline,
        budget_minor=body.budget_minor,
        paid_channels=[str(c) for c in body.paid_channels] if body.paid_channels else None,
        channels=body.channels,
    )
    return {"metric": metric, **forecast(inp).as_dict()}


@router.get("/goals")
async def goals(ctx: BusinessDep) -> dict[str, Any]:
    rows = (
        (
            await ctx.session.execute(
                select(Goal).where(Goal.business_id == ctx.business_id).order_by(Goal.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [goal_view(g) for g in rows],
        "active": next((goal_view(g) for g in rows if g.status == "active"), None),
    }


@router.post("/goals", status_code=status.HTTP_201_CREATED)
async def new_goal(body: GoalIn, ctx: EditorDep) -> dict[str, Any]:
    require_feature(ctx.business, "goals")
    metric = body.metric or DEFAULT_METRIC[body.type]
    actor = Actor.user(ctx.user.id)
    snapshot = await diagnose(ctx.session, ctx.business)
    goal, _ = await create_goal(
        ctx.session,
        ctx.business,
        goal_type=body.type,
        metric=metric,
        target=body.target,
        deadline=body.deadline,
        budget_minor=body.budget_minor,
        title=body.title,
        growth_approach=body.growth_approach,
        approval_mode=body.approval_mode,
        guardrail=body.guardrail,
        stop_rules=body.stop_rules,
        channels=body.channels,
        paid_channels=[str(c) for c in body.paid_channels] if body.paid_channels else None,
        diagnosis=snapshot,
        ai_credit_budget_cents=body.ai_credit_budget_cents,
        actor=actor,
    )
    for spec in body.restrictions:
        kind = str(spec.get("kind", ""))
        params = restrictions_svc.validate(kind, dict(spec.get("params", {})))
        ctx.session.add(Restriction(business_id=ctx.business_id, goal_id=goal.id, kind=kind, params=params))
    if body.activate:
        await activate_goal(ctx.session, ctx.business, goal, actor=actor, replace=body.replace_active)
    return goal_view(goal)


@router.get("/goals/{goal_id}")
async def get_goal(goal_id: uuid.UUID, ctx: BusinessDep) -> dict[str, Any]:
    goal = await ctx.session.get(Goal, goal_id)
    if goal is None:
        raise NotFound("Goal not found")
    history = (
        await ctx.session.execute(
            select(LedgerEvent.occurred_at, LedgerEvent.data)
            .where(
                LedgerEvent.business_id == ctx.business_id,
                LedgerEvent.type == "gtm.goal.paced",
                LedgerEvent.subject_id == goal.id,
            )
            .order_by(LedgerEvent.seq)
            .limit(400)
        )
    ).all()
    rules = (await ctx.session.execute(select(Restriction).where(Restriction.goal_id == goal.id))).scalars().all()
    campaigns = (
        (await ctx.session.execute(select(Campaign).where(Campaign.goal_id == goal.id).order_by(Campaign.created_at)))
        .scalars()
        .all()
    )
    return {
        **goal_view(goal),
        "pace_history": [{"at": at.isoformat(), **data} for at, data in history],
        "restrictions": [restrictions_svc.restriction_view(r) for r in rules],
        "campaigns": [campaign_view(c) for c in campaigns],
    }


class GoalAction(BaseModel):
    replace_active: bool = False
    reason: str | None = Field(default=None, max_length=300)


@router.post("/goals/{goal_id}/activate")
async def activate(goal_id: uuid.UUID, body: GoalAction, ctx: EditorDep) -> dict[str, Any]:
    goal = await ctx.session.get(Goal, goal_id)
    if goal is None:
        raise NotFound("Goal not found")
    await activate_goal(ctx.session, ctx.business, goal, actor=Actor.user(ctx.user.id), replace=body.replace_active)
    return goal_view(goal)


@router.post("/goals/{goal_id}/pause")
async def pause_goal(goal_id: uuid.UUID, ctx: EditorDep) -> dict[str, Any]:
    goal = await ctx.session.get(Goal, goal_id)
    if goal is None:
        raise NotFound("Goal not found")
    if goal.status == "active":
        goal.status = "paused"
    return goal_view(goal)


@router.post("/goals/{goal_id}/stop")
async def stop_goal(goal_id: uuid.UUID, body: GoalAction, ctx: EditorDep) -> dict[str, Any]:
    goal = await ctx.session.get(Goal, goal_id)
    if goal is None:
        raise NotFound("Goal not found")
    await end_goal(
        ctx.session,
        ctx.business,
        goal,
        status="stopped",
        reason=body.reason or "stopped by owner",
        actor=Actor.user(ctx.user.id),
    )
    return goal_view(goal)


@router.post("/goals/{goal_id}/pace")
async def pace_now(goal_id: uuid.UUID, ctx: EditorDep) -> dict[str, Any]:
    goal = await ctx.session.get(Goal, goal_id)
    if goal is None or goal.status != "active":
        raise NotFound("Active goal not found")
    return await pace_goal(ctx.session, ctx.business, goal)


class RestrictionIn(BaseModel):
    kind: str
    params: dict[str, Any] = Field(default_factory=dict)
    goal_id: uuid.UUID | None = None
    note: str | None = Field(default=None, max_length=300)


@router.get("/restrictions")
async def list_restrictions(ctx: BusinessDep) -> list[dict[str, Any]]:
    rows = (
        (
            await ctx.session.execute(
                select(Restriction)
                .where(Restriction.business_id == ctx.business_id)
                .order_by(Restriction.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [restrictions_svc.restriction_view(r) for r in rows]


@router.post("/restrictions", status_code=status.HTTP_201_CREATED)
async def add_restriction(body: RestrictionIn, ctx: EditorDep) -> dict[str, Any]:
    params = restrictions_svc.validate(body.kind, body.params)
    r = Restriction(business_id=ctx.business_id, goal_id=body.goal_id, kind=body.kind, params=params, note=body.note)
    ctx.session.add(r)
    await ctx.session.flush()
    return restrictions_svc.restriction_view(r)


@router.delete("/restrictions/{restriction_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_restriction(restriction_id: uuid.UUID, ctx: EditorDep) -> None:
    r = await ctx.session.get(Restriction, restriction_id)
    if r is None:
        raise NotFound("Restriction not found")
    r.active = False


# ------------------------------------------------------------------ campaigns


@router.get("/campaigns/library")
async def campaign_library(ctx: BusinessDep, category: str | None = None, goal: str | None = None) -> dict[str, Any]:
    return {"categories": list(CATEGORIES), "items": [p.view() for p in playbooks(category=category, goal=goal)]}


@router.get("/campaigns/recommendations")
async def campaign_recommendations(
    ctx: BusinessDep, goal_id: uuid.UUID | None = None, goal_type: GoalType | None = None, launching: bool = False
) -> dict[str, Any]:
    goal = await ctx.session.get(Goal, goal_id) if goal_id else None
    numbers = (await diagnose(ctx.session, ctx.business))["numbers"]
    audience = await ctx.session.scalar(
        select(Audience).where(Audience.business_id == ctx.business_id, Audience.is_primary.is_(True))
    )
    sel = SelectionContext(
        goal_type=goal.type if goal else (goal_type or "demand"),
        business_model=ctx.business.business_model,
        plan=ctx.business.plan,
        followers=int(numbers["followers"]),
        budget_minor=goal.budget_minor if goal else 0,
        b2b=bool(audience and audience.segment == "b2b"),
        customers=int(numbers["customers"]),
        leads=int(numbers["leads_total"]),
        launching=launching,
        local=bool((ctx.business.settings or {}).get("gtm", {}).get("local")),
    )
    return recommend(sel)


class CampaignIn(BaseModel):
    playbook_key: str
    goal_id: uuid.UUID | None = None
    role: Literal["main", "supporting"] = "main"
    name: str | None = Field(default=None, max_length=200)
    budget_minor: int = Field(default=0, ge=0)
    channels: list[str] | None = None
    start: bool = False


class CampaignPatch(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    channels: list[str] | None = None
    frequency: dict[str, float] | None = None
    value_ratio: float | None = Field(default=None, gt=0, le=20)
    budget_minor: int | None = Field(default=None, ge=0)
    status: Literal["active", "paused", "stopped", "completed"] | None = None
    reason: str | None = Field(default=None, max_length=300)


@router.get("/campaigns")
async def campaigns(ctx: BusinessDep, status_filter: str | None = None) -> dict[str, Any]:
    stmt = select(Campaign).where(Campaign.business_id == ctx.business_id)
    if status_filter:
        stmt = stmt.where(Campaign.status.in_(status_filter.split(",")))
    rows = (await ctx.session.execute(stmt.order_by(Campaign.created_at.desc()))).scalars().all()
    return {"items": [campaign_view(c) for c in rows]}


@router.post("/campaigns", status_code=status.HTTP_201_CREATED)
async def new_campaign(body: CampaignIn, ctx: EditorDep) -> dict[str, Any]:
    if get_playbook(body.playbook_key) is None:
        raise NotFound("Unknown playbook")
    campaign = await create_campaign(
        ctx.session,
        ctx.business,
        playbook_key=body.playbook_key,
        goal_id=body.goal_id,
        role=body.role,
        name=body.name,
        budget_minor=body.budget_minor,
        channels=body.channels,
    )
    if body.start:
        await start_campaign(ctx.session, ctx.business, campaign, actor=Actor.user(ctx.user.id))
    return campaign_view(campaign)


@router.get("/campaigns/{campaign_id}")
async def get_campaign(campaign_id: uuid.UUID, ctx: BusinessDep) -> dict[str, Any]:
    campaign = await ctx.session.get(Campaign, campaign_id)
    if campaign is None:
        raise NotFound("Campaign not found")
    return {**campaign_view(campaign), "playbook": (p.view() if (p := get_playbook(campaign.playbook_key)) else None)}


@router.patch("/campaigns/{campaign_id}")
async def update_campaign(campaign_id: uuid.UUID, body: CampaignPatch, ctx: EditorDep) -> dict[str, Any]:
    campaign = await ctx.session.get(Campaign, campaign_id)
    if campaign is None:
        raise NotFound("Campaign not found")
    for key in ("name", "channels", "frequency", "value_ratio", "budget_minor"):
        value = getattr(body, key)
        if value is not None:
            setattr(campaign, key, value)
    if body.status and body.status != campaign.status:
        await set_status(
            ctx.session, ctx.business, campaign, body.status, actor=Actor.user(ctx.user.id), reason=body.reason
        )
    return campaign_view(campaign)
