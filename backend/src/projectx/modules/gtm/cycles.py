"""GTM's part of the daily and weekly cycles, and what it contributes to the founder's cockpit."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.money import Money
from projectx.kernel.time import today_in, utcnow
from projectx.modules.gtm.crm.automations import mark_inactive_customers
from projectx.modules.gtm.goals.metrics import churn_rate, new_customers, new_leads, new_subscribers, revenue
from projectx.modules.gtm.goals.service import goal_view, pace_all
from projectx.modules.gtm.models import Campaign, ChannelAccount, ContentItem, Goal, Post
from projectx.services import Services
from projectx.spine.jobs.queue import enqueue
from projectx.spine.modules import BriefingItem
from projectx.spine.tenancy.models import Business
from projectx.spine.twin.models import STAGES, Contact


async def daily_cycle(services: Services, business_id: uuid.UUID) -> None:
    async with services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        if business is None:
            return
        await pace_all(session, business)
        await mark_inactive_customers(session, business)
        day = today_in(business.timezone).isoformat()
        campaigns = (
            (
                await session.execute(
                    select(Campaign.id).where(Campaign.business_id == business_id, Campaign.status == "active")
                )
            )
            .scalars()
            .all()
        )
        for campaign_id in campaigns:
            await enqueue(
                session,
                "gtm.plan_content",
                {"campaign_id": str(campaign_id), "days": 7},
                business_id=business_id,
                idempotency_key=f"plan:{campaign_id}:{day}",
            )
        await enqueue(
            session, "gtm.build_newsletter", business_id=business_id, idempotency_key=f"newsletter:{business_id}:{day}"
        )


async def weekly_cycle(services: Services, business_id: uuid.UUID) -> None:
    async with services.db.tenant(business_id) as session:
        week = today_in((await session.get(Business, business_id)).timezone).isocalendar()  # type: ignore[union-attr]
        await enqueue(
            session,
            "gtm.weekly_report",
            business_id=business_id,
            idempotency_key=f"report:{business_id}:{week.year}-{week.week}",
        )


async def numbers(session: AsyncSession, business_id: uuid.UUID) -> dict[str, Any]:
    """The three numbers (revenue, profit, churn) as far as GTM can see them, plus the funnel."""
    business = await session.get(Business, business_id)
    now = utcnow()
    month = now - timedelta(days=30)
    previous = month - timedelta(days=30)
    currency = business.currency if business else "USD"
    rev = await revenue(session, business_id, month, now)
    rev_prev = await revenue(session, business_id, previous, month)
    customers = int(
        await session.scalar(
            select(func.count())
            .select_from(Contact)
            .where(
                Contact.business_id == business_id,
                Contact.stage.in_(STAGES[STAGES.index("customer") :]),
                Contact.erased_at.is_(None),
            )
        )
        or 0
    )
    return {
        "revenue": {
            "value_minor": int(rev),
            "currency": currency,
            "display": Money(int(rev), currency).format(),
            "previous_minor": int(rev_prev),
            "window_days": 30,
        },
        "churn": {"rate": await churn_rate(session, business_id, month, now), "window_days": 30},
        "customers": customers,
        "new_leads_30d": int(await new_leads(session, business_id, month, now)),
        "new_subscribers_30d": int(await new_subscribers(session, business_id, month, now)),
        "new_customers_30d": int(await new_customers(session, business_id, month, now)),
    }


async def briefing(session: AsyncSession, business_id: uuid.UUID) -> list[BriefingItem]:
    items: list[BriefingItem] = []
    goal = await session.scalar(select(Goal).where(Goal.business_id == business_id, Goal.status == "active"))
    if goal is not None and goal.pace:
        status = goal.pace.get("status", "on_track")
        level = {"behind": "warning", "at_risk": "critical", "ahead": "success"}.get(status, "info")
        items.append(
            BriefingItem(
                "goal_pace",
                goal.title,
                f"{goal.pace.get('actual', 0):,.0f} so far; on course for about "
                f"{goal.pace.get('projected', 0):,.0f} of {goal.target_value:,.0f}.",
                level=level,
                link=f"/goals/{goal.id}",
                data=goal_view(goal),
            )
        )
    elif goal is None:
        items.append(
            BriefingItem(
                "no_goal",
                "Set your first goal",
                "Pick one outcome and a deadline; the system plans everything around it.",
                link="/goals/new",
            )
        )
    yesterday = utcnow() - timedelta(days=1)
    published = int(
        await session.scalar(
            select(func.count())
            .select_from(Post)
            .where(Post.business_id == business_id, Post.status == "published", Post.published_at >= yesterday)
        )
        or 0
    )
    failed = int(
        await session.scalar(
            select(func.count())
            .select_from(Post)
            .where(Post.business_id == business_id, Post.status == "failed", Post.updated_at >= yesterday)
        )
        or 0
    )
    if published:
        items.append(
            BriefingItem(
                "published",
                f"{published} post{'s' if published != 1 else ''} went out",
                "Verified on each platform.",
                level="success",
                link="/content",
            )
        )
    if failed:
        items.append(
            BriefingItem(
                "failed",
                f"{failed} post{'s' if failed != 1 else ''} failed",
                "Open them to retry or edit.",
                level="warning",
                link="/channels?tab=queue",
            )
        )
    review = int(
        await session.scalar(
            select(func.count())
            .select_from(ContentItem)
            .where(ContentItem.business_id == business_id, ContentItem.status == "needs_review")
        )
        or 0
    )
    if review:
        items.append(
            BriefingItem(
                "review",
                f"{review} piece{'s' if review != 1 else ''} waiting for you",
                "Approve, edit or skip.",
                link="/decisions",
            )
        )
    expired = (
        (
            await session.execute(
                select(ChannelAccount.name).where(
                    ChannelAccount.business_id == business_id, ChannelAccount.status == "expired"
                )
            )
        )
        .scalars()
        .all()
    )
    for name in expired:
        items.append(
            BriefingItem(
                "reconnect",
                f"Reconnect {name}",
                "Posting there is paused until you do.",
                level="critical",
                link="/channels",
            )
        )
    leads = int(await new_leads(session, business_id, yesterday, utcnow()))
    if leads:
        items.append(
            BriefingItem(
                "leads", f"{leads} new lead{'s' if leads != 1 else ''} yesterday", "", level="success", link="/audience"
            )
        )
    return items
