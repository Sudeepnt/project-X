"""The content pipeline: plan → write → adapt per platform → check → propose for publishing.

Each step is a job, so a slow model call never blocks a request and every failure retries on its
own. Nothing reaches a platform without passing the checks and the policy engine.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.time import today_in, utcnow
from projectx.modules.gtm.brand.service import current_book
from projectx.modules.gtm.content.checks import run_checks
from projectx.modules.gtm.content.generate import adapt_variants, write_core
from projectx.modules.gtm.content.planner import plan_slots
from projectx.modules.gtm.goals import restrictions as restrictions_svc
from projectx.modules.gtm.growth.links import create_link, short_url
from projectx.modules.gtm.models import Campaign, ChannelAccount, ContentItem, ContentTheme, ContentVariant, LandingPage
from projectx.modules.gtm.platforms import PLATFORMS
from projectx.services import Services
from projectx.spine.decisions.service import Proposal, propose
from projectx.spine.gateway.contracts import ComputeCapReached, ModelsRateLimited
from projectx.spine.jobs.queue import enqueue
from projectx.spine.jobs.registry import JobContext, RetryLater, job
from projectx.spine.ledger.service import Actor, append
from projectx.spine.memory.service import as_prompt_notes, recall
from projectx.spine.tenancy.models import Business

AGENT = Actor.agent("pack:gtm.content")


def _scheduled_text(when: datetime, timezone: str) -> str:
    """'Scheduled for Sun 29 Nov, 9:00 AM IST': the founder's own clock, not UTC."""
    try:
        local = when.astimezone(ZoneInfo(timezone))
    except (KeyError, ValueError):
        local = when
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"Scheduled for {local:%a %d %b}, {hour}:{local:%M %p} {local.tzname() or ''}".rstrip()


@job("gtm.plan_content", max_attempts=3, timeout_s=300, priority=50)
async def plan_content(ctx: JobContext) -> dict[str, Any] | None:
    business_id = ctx.require_business()
    days = int(ctx.payload.get("days", 7))
    created: list[uuid.UUID] = []
    async with ctx.services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        campaign = await session.get(Campaign, uuid.UUID(ctx.payload["campaign_id"]))
        if business is None or campaign is None or campaign.status != "active":
            return None
        slots = await plan_slots(session, business, campaign, start_day=today_in(business.timezone), days=days)
        for slot in slots:
            item = ContentItem(
                business_id=business_id,
                campaign_id=campaign.id,
                theme_id=slot.theme_id,
                pillar=slot.pillar,
                intent=slot.intent,
                hook_type=slot.hook_type,
                title=f"{slot.theme_name}",
                core_idea="",
                status="idea",
                planned_for=slot.when,
                source_ref={
                    "platforms": slot.platforms,
                    "formats": slot.formats,
                    "mix_kind": slot.mix_kind,
                    "moment": slot.moment,
                    "theme_name": slot.theme_name,
                },
            )
            session.add(item)
            await session.flush()
            created.append(item.id)
            # Generate close to posting time so drafts use fresh learning, but early enough for review.
            run_at = max(utcnow(), slot.when - timedelta(hours=24))
            await enqueue(
                session,
                "gtm.generate_content",
                {"content_id": str(item.id)},
                business_id=business_id,
                run_at=run_at,
                idempotency_key=f"gen:{item.id}",
            )
    return {"planned": len(created)}


async def _landing_link(
    session: AsyncSession, business: Business, item: ContentItem, platform: str, services: Services
) -> str | None:
    if item.intent != "ask" and platform not in ("newsletter", "telegram", "blog"):
        return None
    page = None
    if item.campaign_id:
        page = await session.scalar(
            select(LandingPage).where(LandingPage.campaign_id == item.campaign_id, LandingPage.status == "published")
        )
    destination = f"{services.settings.base_url}/p/{business.slug}/{page.slug}" if page else business.website
    if not destination:
        return None
    link = await create_link(
        session,
        business,
        destination=destination,
        platform=platform,
        campaign_id=item.campaign_id,
        content_id=item.id,
        label=item.title[:200],
    )
    return short_url(services.settings.base_url, link.slug)


@job("gtm.generate_content", max_attempts=5, timeout_s=600, priority=40)
async def generate_content(ctx: JobContext) -> dict[str, Any] | None:
    business_id = ctx.require_business()
    content_id = uuid.UUID(ctx.payload["content_id"])
    services = ctx.services
    async with services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        item = await session.get(ContentItem, content_id)
        if business is None or item is None or item.status not in ("idea", "draft"):
            return None
        book = await current_book(session, business_id)
        campaign = await session.get(Campaign, item.campaign_id) if item.campaign_id else None
        theme = await session.get(ContentTheme, item.theme_id) if item.theme_id else None
        lessons = as_prompt_notes(await recall(session, business_id, shelf="case", kind="decision_rejected", limit=5))
        source = item.source_ref or {}
        slot = {
            "theme_name": theme.name if theme else source.get("theme_name", "General"),
            "pillar": item.pillar,
            "intent": item.intent,
            "hook_type": item.hook_type,
            "mix_kind": source.get("mix_kind", "text"),
            "moment": source.get("moment"),
            "campaign_brief": campaign.brief if campaign else None,
        }
        formats: dict[str, str] = dict(source.get("formats") or {})
        # Imported stories and the owner's own ideas are source material: the only facts a post may use.
        story = source if item.source_type == "imported" or source.get("summary") else None
    try:
        core = await write_core(services, business, book, slot=slot, source=story, notes=lessons)
        async with services.db.tenant(business_id) as session:
            biz = await session.get(Business, business_id)
            it = await session.get(ContentItem, content_id)
            assert biz is not None
            assert it is not None
            links = {p: await _landing_link(session, biz, it, p, services) for p in formats}
        link = next((v for v in links.values() if v), None)
        drafts = await adapt_variants(services, business, book, core, formats=formats, link=link)
    except ComputeCapReached as exc:
        raise RetryLater(3600, "daily compute cap reached") from exc
    except ModelsRateLimited as exc:
        raise RetryLater(exc.retry_after, "model provider rate limit") from exc

    async with services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        item = await session.get(ContentItem, content_id)
        assert business is not None
        assert item is not None
        item.title, item.core_idea, item.body, item.cta = core.title, core.core_idea, core.body, core.cta
        item.claims = core.claims
        item.brand_book_version = book.version if book else None
        item.status = "draft"
        by_platform = {d.platform: d for d in drafts.variants}
        variants: list[ContentVariant] = []
        for platform, fmt in formats.items():
            draft = by_platform.get(platform)
            if draft is None:
                continue
            variant = ContentVariant(
                business_id=business_id,
                content_id=item.id,
                platform=platform,
                format=fmt,
                title=draft.title or None,
                text=draft.text,
                thread=draft.thread,
                hashtags=draft.hashtags,
                media=[],
                status="draft",
                scheduled_at=item.planned_for,
            )
            session.add(variant)
            variants.append(variant)
        await session.flush()
        results = await run_checks(session, business, book, item, variants)
        item.checks = results
        await append(
            session,
            business_id=business_id,
            type="gtm.content.changed",
            actor=AGENT,
            subject=("content", item.id),
            data={
                "content_id": str(item.id),
                "status": "draft",
                "intent": item.intent,
                "campaign_id": str(item.campaign_id) if item.campaign_id else None,
            },
        )
        await propose_publication(session, business, item, variants, results)
    return {"content_id": str(content_id), "outcome": results["outcome"]}


async def _is_new_kind(session: AsyncSession, item: ContentItem) -> bool:
    published = await session.scalar(
        select(ContentItem.id)
        .where(
            ContentItem.business_id == item.business_id,
            ContentItem.campaign_id == item.campaign_id,
            ContentItem.intent == item.intent,
            ContentItem.status.in_(("scheduled", "published")),
            ContentItem.id != item.id,
        )
        .limit(1)
    )
    return published is None


async def propose_publication(
    session: AsyncSession,
    business: Business,
    item: ContentItem,
    variants: list[ContentVariant],
    results: dict[str, Any],
) -> None:
    """One decision per idea (all its platform versions), so the founder's queue stays short."""
    if results["outcome"] == "blocked":
        item.status = "rejected"
        return
    accounts = {
        a.platform: a
        for a in (
            await session.execute(
                select(ChannelAccount).where(
                    ChannelAccount.business_id == business.id, ChannelAccount.status.in_(("connected", "practice"))
                )
            )
        ).scalars()
    }
    api_variants, manual_variants = [], []
    for v in variants:
        if v.status == "blocked":
            continue
        spec = PLATFORMS.get(v.platform)
        account = accounts.get(v.platform)
        if v.platform in ("newsletter",):
            v.status = "ready"
            continue  # collected into the next newsletter issue
        if spec and (spec.api_posting == "manual_only" or account is None):
            manual_variants.append(v)
            continue
        if (
            spec
            and spec.api_posting == "unaudited_private"
            and account
            and not business.practice_mode
            and not (account.capabilities or {}).get("audited")
        ):
            manual_variants.append(v)
            continue
        v.account_id = account.id if account else None
        api_variants.append(v)
    when = item.planned_for or utcnow()
    rules = await restrictions_svc.active(session, business.id)
    violations = []
    for v in api_variants:
        violations += await restrictions_svc.publish_violations(
            session, business.id, platform=v.platform, when=when, tz=business.timezone, restrictions=rules
        )
    review_needed = results["outcome"] == "review"
    if api_variants:
        from projectx.spine.policy.engine import Violation

        if review_needed:
            violations.append(
                Violation("checks_flagged", "The checks flagged something for a person to look at", hard=False)
            )
        outcome = await propose(
            session,
            business.id,
            Proposal(
                action_type="gtm.publish_post",
                title=item.title,
                summary=item.core_idea or item.body[:280],
                payload={"content_id": str(item.id), "variant_ids": [str(v.id) for v in api_variants]},
                preview={
                    "intent": item.intent,
                    "pillar": item.pillar,
                    "checks": results,
                    "variants": [
                        {
                            "id": str(v.id),
                            "platform": v.platform,
                            "format": v.format,
                            "text": v.text,
                            "thread": v.thread,
                            "title": v.title,
                            "spec": v.spec,
                        }
                        for v in api_variants
                    ],
                },
                new_kind=await _is_new_kind(session, item),
                violations=tuple(violations),
                why_now=_scheduled_text(when, business.timezone),
                if_no="It won't be posted; the slot stays empty.",
                undo="Delete the posts from each platform.",
                subject=("content", item.id),
                requested_by="pack:gtm.content",
                priority=40,
                expires_at=when + timedelta(hours=12),
            ),
        )
        item.decision_id = outcome.decision_id
        item.status = {
            "running": "approved",
            "waiting_for_approval": "needs_review",
            "blocked": "rejected",
            "manual_task": "needs_review",
        }[outcome.status]
    for v in manual_variants:
        spec = PLATFORMS.get(v.platform)
        reason = (
            "Connect the account to post automatically."
            if v.platform not in accounts
            else f"{spec.label if spec else v.platform} doesn't allow automatic posting yet."
        )
        await propose(
            session,
            business.id,
            Proposal(
                action_type="gtm.publish_manual",
                title=f"Post on {spec.label if spec else v.platform}: {item.title}",
                summary=reason,
                payload={"variant_id": str(v.id)},
                preview={"platform": v.platform, "format": v.format, "text": v.text, "thread": v.thread},
                subject=("variant", v.id),
                requested_by="pack:gtm.content",
                priority=60,
                expires_at=when + timedelta(days=2),
            ),
        )
        v.status = "manual"
    if not api_variants and item.status == "draft":
        item.status = "needs_review" if manual_variants else "approved"
