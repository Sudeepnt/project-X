"""Plans, limits and feature gates. One bill per business, whatever modules it uses.

GTM tiers follow the GTM plan: Autopilot (AI makes and posts the content), Growth (goals,
campaign library, CRM, nurture, learning) and Scale (ads, outreach, partnerships, deeper
attribution). Ad spend is never inside the price: Meta and Google bill the customer directly;
Scale adds a small management share of ad spend. Prices are starting points to test.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.errors import QuotaExceeded
from projectx.spine.billing.models import UsageCounter
from projectx.spine.tenancy.models import Business

UNLIMITED = None


@dataclass(frozen=True)
class Plan:
    key: str
    name: str
    tagline: str
    price_usd_month: int
    limits: dict[str, int | None]
    features: frozenset[str]
    ad_spend_share_bps: int = 0
    highlights: tuple[str, ...] = field(default_factory=tuple)


_BASE = frozenset({"brand_book", "content_calendar", "publishing", "basic_analytics", "approvals", "landing_pages"})
_AUTOPILOT = _BASE | {"repurposing", "newsletter", "link_tracking", "holiday_calendar"}
_GROWTH = _AUTOPILOT | {
    "goals",
    "campaign_library",
    "pacing",
    "crm",
    "sequences",
    "whatsapp",
    "discord_bot",
    "learning",
    "experiments",
    "inbox",
    "weekly_report",
    "public_dashboard",
    "media_kit",
}
_SCALE = _GROWTH | {"ads", "retargeting", "outreach", "partnerships", "attribution_advanced", "priority_review"}

PLANS: dict[str, Plan] = {
    "free": Plan(
        "free",
        "Free",
        "Try it on one channel",
        0,
        {
            "platforms": 1,
            "posts_per_month": 10,
            "ai_daily_cents": 25,
            "contacts": 250,
            "seats": 1,
            "landing_pages": 1,
            "emails_per_month": 500,
        },
        _BASE,
        highlights=("1 channel", "About 10 posts a month", "Brand book and calendar"),
    ),
    "autopilot": Plan(
        "autopilot",
        "Autopilot",
        "AI makes and posts your content",
        29,
        {
            "platforms": 3,
            "posts_per_month": 90,
            "ai_daily_cents": 100,
            "contacts": 1_000,
            "seats": 2,
            "landing_pages": 3,
            "emails_per_month": 5_000,
        },
        _AUTOPILOT,
        highlights=("3 channels", "90 posts a month", "Newsletter and tracked links"),
    ),
    "growth": Plan(
        "growth",
        "Growth",
        "Goals, campaigns, CRM and nurture that learn",
        99,
        {
            "platforms": 8,
            "posts_per_month": 400,
            "ai_daily_cents": 400,
            "contacts": 10_000,
            "seats": 5,
            "landing_pages": 25,
            "emails_per_month": 50_000,
        },
        _GROWTH,
        highlights=("Goal engine and pacing", "Campaign library", "CRM, sequences, Discord bot"),
    ),
    "scale": Plan(
        "scale",
        "Scale",
        "Ads, outreach and partnerships on top",
        299,
        {
            "platforms": UNLIMITED,
            "posts_per_month": 2_000,
            "ai_daily_cents": 1_500,
            "contacts": 100_000,
            "seats": 15,
            "landing_pages": UNLIMITED,
            "emails_per_month": 250_000,
        },
        _SCALE,
        ad_spend_share_bps=700,
        highlights=("Meta and Google ads with caps", "Outreach and partnerships", "Priority human review"),
    ),
}

PLAN_ORDER = ("free", "autopilot", "growth", "scale")


def plan_for(key: str) -> Plan:
    return PLANS.get(key, PLANS["free"])


def cheapest_plan_with(feature: str) -> Plan | None:
    for key in PLAN_ORDER:
        if feature in PLANS[key].features:
            return PLANS[key]
    return None


def require_feature(business: Business, feature: str) -> None:
    plan = plan_for(business.plan)
    if feature not in plan.features:
        upgrade = cheapest_plan_with(feature)
        hint = f" Upgrade to {upgrade.name} to use it." if upgrade else ""
        raise QuotaExceeded(
            f"Your {plan.name} plan doesn't include this.{hint}",
            extra={"feature": feature, "upgrade_to": upgrade.key if upgrade else None},
        )


def current_period(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m")


async def usage(session: AsyncSession, business_id: uuid.UUID, metric: str, period: str | None = None) -> int:
    value = await session.scalar(
        select(UsageCounter.value).where(
            UsageCounter.business_id == business_id,
            UsageCounter.metric == metric,
            UsageCounter.period == (period or current_period()),
        )
    )
    return int(value or 0)


async def check_quota(session: AsyncSession, business: Business, metric: str, amount: int = 1) -> None:
    limit = plan_for(business.plan).limits.get(metric)
    if limit is None:
        return
    used = await usage(session, business.id, metric)
    if used + amount > limit:
        raise QuotaExceeded(
            f"You've used {used} of {limit} {metric.replace('_', ' ')} this month.",
            extra={"metric": metric, "limit": limit, "used": used},
        )


async def add_usage(session: AsyncSession, business_id: uuid.UUID, metric: str, amount: int = 1) -> int:
    stmt = insert(UsageCounter).values(business_id=business_id, metric=metric, period=current_period(), value=amount)
    stmt = stmt.on_conflict_do_update(
        index_elements=[UsageCounter.business_id, UsageCounter.metric, UsageCounter.period],
        set_={"value": UsageCounter.value + amount},
    )
    await session.execute(stmt)
    return await usage(session, business_id, metric)
