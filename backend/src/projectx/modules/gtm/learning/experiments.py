"""A/B lab for posts: two versions of the same idea, same platform, same hour, two days apart.

Social platforms don't split one post's audience at random, so this is an honest approximation: both versions
go to the same account at the same time of day, and we compare engagement per person reached. A winner is
declared only with strong evidence (high probability *and* enough reach on both); otherwise it says so.
Results are labelled indicative, and what was learned feeds the planner's memory.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.api.deps import BusinessDep, EditorDep
from projectx.kernel.errors import Conflict, NotFound, ValidationFailed
from projectx.kernel.time import utcnow
from projectx.modules.gtm.learning.bandit import prob_best
from projectx.modules.gtm.models import ContentItem, ContentVariant, Experiment, ExperimentVariant, Post
from projectx.modules.gtm.platforms import PLATFORMS
from projectx.services import Services
from projectx.spine.decisions.models import Decision
from projectx.spine.jobs.registry import periodic
from projectx.spine.ledger.service import Actor, append
from projectx.spine.memory.service import remember
from projectx.spine.notifications.service import notify

router = APIRouter(prefix="/api/v1/b/{business_id}/gtm/experiments", tags=["gtm: experiments"])
GAP = timedelta(days=2)
MIN_REACH = 300
CONFIDENCE = 0.95
MAX_DAYS = 14
AGENT = Actor.agent("pack:gtm.learning")


class HookTestIn(BaseModel):
    content_id: uuid.UUID
    platform: str = Field(min_length=1, max_length=30)
    text_b: str = Field(min_length=1, max_length=10_000)
    hypothesis: str = Field(default="", max_length=500)


async def start_hook_test(session: AsyncSession, business_id: uuid.UUID, body: HookTestIn) -> Experiment:
    item = await session.get(ContentItem, body.content_id)
    if item is None:
        raise NotFound("Content not found")
    if item.status != "needs_review" or item.decision_id is None:
        raise Conflict("Start a test while the post is waiting for approval")
    decision = await session.get(Decision, item.decision_id)
    if decision is None or decision.status != "pending":
        raise Conflict("Start a test while the post is waiting for approval")
    a = await session.scalar(
        select(ContentVariant).where(ContentVariant.content_id == item.id, ContentVariant.platform == body.platform)
    )
    if a is None:
        raise ValidationFailed("This post has no version for that platform")
    if body.text_b.strip() == a.text.strip():
        raise ValidationFailed("Version B must differ from version A")
    existing = await session.scalar(
        select(Experiment.id).where(Experiment.subject_type == "content_variant", Experiment.subject_id == a.id)
    )
    if existing:
        raise Conflict("This version is already being tested")
    # Variants are unique per platform within an item, so version B lives on a linked sibling item that
    # shares the same approval.
    sibling = ContentItem(
        business_id=business_id,
        campaign_id=item.campaign_id,
        theme_id=item.theme_id,
        pillar=item.pillar,
        intent=item.intent,
        hook_type=item.hook_type,
        title=f"{item.title} (version B)",
        core_idea=item.core_idea,
        body=body.text_b.strip(),
        cta=item.cta,
        source_type="ab_test",
        status="needs_review",
        planned_for=(item.planned_for or utcnow()) + GAP,
        decision_id=decision.id,
        checks=dict(item.checks or {}),
        source_ref={"ab_of": str(item.id), "platforms": [a.platform]},
    )
    session.add(sibling)
    await session.flush()
    b = ContentVariant(
        business_id=business_id,
        content_id=sibling.id,
        platform=a.platform,
        format=a.format,
        title=a.title,
        text=body.text_b.strip(),
        thread=list(a.thread or []),
        media=list(a.media or []),
        hashtags=list(a.hashtags or []),
        spec=dict(a.spec or {}),
        status=a.status,
        account_id=a.account_id,
        scheduled_at=(a.scheduled_at or item.planned_for or utcnow()) + GAP,
    )
    session.add(b)
    await session.flush()
    experiment = Experiment(
        business_id=business_id,
        key=f"hook-{a.id.hex[:8]}",
        name=f"{item.title} on {PLATFORMS[a.platform].label if a.platform in PLATFORMS else a.platform}",
        kind="hook",
        hypothesis=body.hypothesis,
        subject_type="content_variant",
        subject_id=a.id,
        metric="engagement_rate",
        min_samples=MIN_REACH,
        status="running",
    )
    session.add(experiment)
    await session.flush()
    session.add_all(
        [
            ExperimentVariant(
                business_id=business_id,
                experiment_id=experiment.id,
                key="A",
                label=a.text[:200],
                config={"variant_id": str(a.id)},
            ),
            ExperimentVariant(
                business_id=business_id,
                experiment_id=experiment.id,
                key="B",
                label=b.text[:200],
                config={"variant_id": str(b.id)},
            ),
        ]
    )
    # Version B joins the same approval: the founder approves (or edits) both at once.
    payload = dict(decision.payload or {})
    payload["variant_ids"] = [*payload.get("variant_ids", []), str(b.id)]
    decision.payload = payload
    preview = dict(decision.preview or {})
    preview["variants"] = [
        *preview.get("variants", []),
        {
            "id": str(b.id),
            "platform": b.platform,
            "format": b.format,
            "text": b.text,
            "title": b.title,
            "thread": b.thread,
            "spec": b.spec,
            "ab": "B",
        },
    ]
    decision.preview = preview
    await append(
        session,
        business_id=business_id,
        type="gtm.experiment.changed",
        actor=AGENT,
        subject=("experiment", experiment.id),
        data={"experiment_id": str(experiment.id), "status": "running"},
    )
    return experiment


async def evaluate(session: AsyncSession, experiment: Experiment) -> dict[str, Any]:
    variants = (
        (await session.execute(select(ExperimentVariant).where(ExperimentVariant.experiment_id == experiment.id)))
        .scalars()
        .all()
    )
    published_at = []
    for v in variants:
        post = await session.scalar(
            select(Post).where(Post.variant_id == uuid.UUID(v.config["variant_id"]), Post.status == "published")
        )
        if post is None:
            continue
        metrics = post.metrics or {}
        v.impressions = int(metrics.get("reach") or metrics.get("impressions") or 0)
        v.conversions = min(int(metrics.get("engagements") or 0), v.impressions)
        if post.published_at:
            published_at.append(post.published_at)
    arms = {v.key: (v.impressions, v.conversions) for v in variants}
    chances = prob_best(arms, samples=3000) if all(t > 0 for t, _ in arms.values()) else {}
    leader = max(chances, key=lambda k: chances[k]) if chances else None
    enough = len(published_at) == len(variants) and all(v.impressions >= experiment.min_samples for v in variants)
    expired = bool(published_at) and utcnow() - max(published_at) > timedelta(days=MAX_DAYS)
    result = {
        "prob_best": {k: round(p, 3) for k, p in chances.items()},
        "leader": leader,
        "rates": {v.key: round(v.conversions / v.impressions, 4) if v.impressions else None for v in variants},
        "indicative": True,
    }
    if leader and enough and chances[leader] >= CONFIDENCE:
        experiment.status, experiment.winner_key, experiment.ended_at = "concluded", leader, utcnow()
    elif expired and len(published_at) == len(variants):
        experiment.status, experiment.ended_at = "no_clear_winner", utcnow()
    experiment.result = result
    return result


@periodic("gtm.evaluate_experiments", every=timedelta(hours=6))
async def evaluate_all(services: Services, now: Any) -> None:
    async with services.db.session() as session:
        running = (
            await session.execute(select(Experiment.business_id, Experiment.id).where(Experiment.status == "running"))
        ).all()
    for business_id, experiment_id in running:
        async with services.db.tenant(business_id) as session:
            experiment = await session.get(Experiment, experiment_id)
            if experiment is None:
                continue
            await evaluate(session, experiment)
            if experiment.status == "running":
                continue
            await append(
                session,
                business_id=business_id,
                type="gtm.experiment.changed",
                actor=AGENT,
                subject=("experiment", experiment.id),
                data={
                    "experiment_id": str(experiment.id),
                    "status": experiment.status,
                    "winner": experiment.winner_key,
                },
            )
            if experiment.winner_key:
                winner = await session.scalar(
                    select(ExperimentVariant).where(
                        ExperimentVariant.experiment_id == experiment.id, ExperimentVariant.key == experiment.winner_key
                    )
                )
                await remember(
                    session,
                    business_id,
                    shelf="gtm",
                    kind="experiment",
                    title=experiment.name,
                    lesson=f"This opening won ({experiment.result.get('rates')}): {winner.label if winner else ''}",
                    outcome=experiment.result,
                    tags=["hook_test"],
                    action_type="gtm.publish_post",
                )
            await notify(
                session,
                business_id,
                kind="experiment.done",
                title=f"Test finished: {experiment.name}",
                body=f"Version {experiment.winner_key} won."
                if experiment.winner_key
                else "No clear winner; both did about the same.",
                link="/analytics?tab=experiments",
            )


def experiment_view(e: Experiment, variants: list[ExperimentVariant]) -> dict[str, Any]:
    return {
        "id": str(e.id),
        "name": e.name,
        "kind": e.kind,
        "hypothesis": e.hypothesis,
        "status": e.status,
        "metric": e.metric,
        "winner": e.winner_key,
        "min_samples": e.min_samples,
        "result": e.result,
        "started_at": e.started_at.isoformat(),
        "ended_at": e.ended_at.isoformat() if e.ended_at else None,
        "variants": [
            {"key": v.key, "label": v.label, "reach": v.impressions, "engagements": v.conversions}
            for v in sorted(variants, key=lambda v: v.key)
        ],
    }


@router.get("")
async def list_experiments(ctx: BusinessDep) -> dict[str, Any]:
    rows = (
        (await ctx.session.execute(select(Experiment).order_by(Experiment.started_at.desc()).limit(100)))
        .scalars()
        .all()
    )
    out = []
    for e in rows:
        variants = (
            (await ctx.session.execute(select(ExperimentVariant).where(ExperimentVariant.experiment_id == e.id)))
            .scalars()
            .all()
        )
        out.append(experiment_view(e, list(variants)))
    return {"items": out, "min_reach": MIN_REACH, "confidence": CONFIDENCE}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_hook_test(body: HookTestIn, ctx: EditorDep) -> dict[str, Any]:
    experiment = await start_hook_test(ctx.session, ctx.business_id, body)
    variants = (
        (await ctx.session.execute(select(ExperimentVariant).where(ExperimentVariant.experiment_id == experiment.id)))
        .scalars()
        .all()
    )
    return experiment_view(experiment, list(variants))
