"""Running campaigns: create from a playbook, start (through the policy engine), pause, stop."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.errors import Conflict, NotFound, ValidationFailed
from projectx.kernel.time import today_in
from projectx.modules.gtm.campaigns.library import get_playbook
from projectx.modules.gtm.config import gtm_settings
from projectx.modules.gtm.models import Campaign, ChannelAccount, Goal, LandingPage
from projectx.spine.billing.plans import plan_for
from projectx.spine.decisions.executors import ExecutionContext, ExecutionResult, executor, undoer
from projectx.spine.jobs.queue import enqueue
from projectx.spine.ledger.service import Actor, append
from projectx.spine.tenancy.models import Business

OWNED_CHANNELS = {"newsletter", "blog", "email"}


async def create_campaign(
    session: AsyncSession,
    business: Business,
    *,
    playbook_key: str,
    goal_id: uuid.UUID | None,
    role: str = "main",
    name: str | None = None,
    budget_minor: int = 0,
    channels: list[str] | None = None,
    starts_on: date | None = None,
    ends_on: date | None = None,
) -> Campaign:
    playbook = get_playbook(playbook_key)
    if playbook is None:
        raise NotFound("Unknown playbook")
    tiers = ("free", "autopilot", "growth", "scale")
    if tiers.index(playbook.tier) > tiers.index(business.plan if business.plan in tiers else "free"):
        raise ValidationFailed(
            f"{playbook.name} needs the {plan_for(playbook.tier).name} plan", extra={"upgrade_to": playbook.tier}
        )
    if playbook.paid and budget_minor <= 0:
        raise ValidationFailed("This campaign needs an ad budget", extra={"field": "budget_minor"})
    goal = await session.get(Goal, goal_id) if goal_id else None
    if goal_id and goal is None:
        raise NotFound("Goal not found")
    connected = set(
        (
            await session.execute(
                select(ChannelAccount.platform).where(
                    ChannelAccount.business_id == business.id, ChannelAccount.status.in_(("connected", "practice"))
                )
            )
        ).scalars()
    )
    chosen = channels or [c for c in playbook.channels if c in connected or c in OWNED_CHANNELS] or playbook.channels
    learned_ratio = gtm_settings(business).get("value_ratio")
    campaign = Campaign(
        business_id=business.id,
        goal_id=goal.id if goal else None,
        playbook_key=playbook.key,
        role=role,
        name=(name or playbook.name)[:200],
        status="draft",
        starts_on=starts_on or today_in(business.timezone),
        ends_on=ends_on or (goal.deadline if goal else None),
        budget_minor=budget_minor,
        budget_split=playbook.budget_split,
        channels=chosen,
        content_mix=playbook.content_mix,
        frequency={k: v for k, v in playbook.frequency.items() if k in chosen or not chosen},
        value_ratio=float(learned_ratio or playbook.value_ratio),
        settings={
            "page_kind": playbook.page_kind,
            "sequence": playbook.sequence,
            "pipeline": playbook.pipeline,
            "stop_rules": playbook.stop_rules,
        },
        brief=f"{playbook.choose_when}. " + " ".join(playbook.steps[:3]),
    )
    session.add(campaign)
    await session.flush()
    return campaign


async def start_campaign(session: AsyncSession, business: Business, campaign: Campaign, *, actor: Actor) -> Campaign:
    if campaign.status == "active":
        return campaign
    if campaign.status in ("completed", "stopped"):
        raise Conflict("This campaign has ended; create a new one")
    campaign.status = "active"
    page_kind = (campaign.settings or {}).get("page_kind")
    if page_kind:
        existing = await session.scalar(
            select(LandingPage.id).where(LandingPage.business_id == business.id, LandingPage.campaign_id == campaign.id)
        )
        if existing is None:
            from projectx.modules.gtm.growth.landing import create_page

            await create_page(
                session,
                business,
                title=campaign.name,
                kind=page_kind,
                campaign_id=campaign.id,
                slug=f"{campaign.playbook_key}-{str(campaign.id)[:6]}",
            )
    await append(
        session,
        business_id=business.id,
        type="gtm.campaign.changed",
        actor=actor,
        subject=("campaign", campaign.id),
        data={"campaign_id": str(campaign.id), "playbook": campaign.playbook_key, "status": "active"},
    )
    await enqueue(
        session,
        "gtm.plan_content",
        {"campaign_id": str(campaign.id)},
        business_id=business.id,
        idempotency_key=f"plan:{campaign.id}:start",
    )
    return campaign


async def set_status(
    session: AsyncSession,
    business: Business,
    campaign: Campaign,
    status: str,
    *,
    actor: Actor,
    reason: str | None = None,
) -> Campaign:
    if status not in ("paused", "active", "stopped", "completed"):
        raise ValidationFailed("Unknown status")
    if status == "active":
        return await start_campaign(session, business, campaign, actor=actor)
    campaign.status = status
    campaign.stop_reason = reason
    await append(
        session,
        business_id=business.id,
        type="gtm.campaign.changed",
        actor=actor,
        subject=("campaign", campaign.id),
        data={"campaign_id": str(campaign.id), "playbook": campaign.playbook_key, "status": status, "reason": reason},
    )
    return campaign


@executor("gtm.start_campaign")
async def _start(ctx: ExecutionContext) -> ExecutionResult:
    async with ctx.services.db.tenant(ctx.business_id) as session:
        business = await session.get(Business, ctx.business_id)
        campaign = await session.get(Campaign, uuid.UUID(ctx.payload["campaign_id"]))
        if business is None or campaign is None:
            raise NotFound("Campaign not found")
        await start_campaign(session, business, campaign, actor=Actor.agent("pack:gtm.campaigns"))
    return ExecutionResult(
        data={"campaign_id": ctx.payload["campaign_id"]},
        summary="Campaign started",
        undo_payload={"campaign_id": ctx.payload["campaign_id"]},
    )


@undoer("gtm.start_campaign")
async def _pause(ctx: ExecutionContext) -> dict[str, Any]:
    async with ctx.services.db.tenant(ctx.business_id) as session:
        business = await session.get(Business, ctx.business_id)
        campaign = await session.get(Campaign, uuid.UUID((ctx.undo_payload or {})["campaign_id"]))
        if business and campaign:
            await set_status(session, business, campaign, "paused", actor=Actor.system("undo"), reason="undone")
    return {"paused": True}


def campaign_view(c: Campaign) -> dict[str, Any]:
    playbook = get_playbook(c.playbook_key)
    return {
        "id": str(c.id),
        "goal_id": str(c.goal_id) if c.goal_id else None,
        "playbook_key": c.playbook_key,
        "playbook_name": playbook.name if playbook else c.playbook_key,
        "category": playbook.category if playbook else None,
        "runs": playbook.runs if playbook else None,
        "role": c.role,
        "name": c.name,
        "status": c.status,
        "starts_on": c.starts_on.isoformat() if c.starts_on else None,
        "ends_on": c.ends_on.isoformat() if c.ends_on else None,
        "budget_minor": c.budget_minor,
        "budget_split": c.budget_split,
        "channels": c.channels,
        "content_mix": c.content_mix,
        "frequency": c.frequency,
        "value_ratio": c.value_ratio,
        "settings": c.settings,
        "metrics": c.metrics,
        "brief": c.brief,
        "stop_reason": c.stop_reason,
        "created_at": c.created_at.isoformat(),
    }
