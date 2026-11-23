"""Diagnose, then set the goal: read the current numbers and recommend one main goal and the
overall growth approach. The owner sets the target, deadline, budget and restrictions."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.time import utcnow
from projectx.modules.gtm.goals.metrics import churn_rate, new_customers, new_leads, revenue
from projectx.modules.gtm.models import ChannelAccount
from projectx.spine.tenancy.models import Business
from projectx.spine.twin.models import STAGES, Contact

APPROACH_BY_MODEL = {
    "media": "content_community",
    "creator": "content_community",
    "service": "sales_referrals",
    "software": "product_led",
    "ecommerce": "content_community",
    "other": "sales_referrals",
}
APPROACH_LABEL = {
    "content_community": "Content- and community-led",
    "sales_referrals": "Sales-led plus referrals",
    "partner_led": "Partner-led",
    "product_led": "Product-led, with a free way in",
}


async def snapshot(session: AsyncSession, business: Business) -> dict[str, Any]:
    bid = business.id
    now = utcnow()
    month_ago = now - timedelta(days=30)
    accounts = (
        await session.execute(
            select(ChannelAccount.platform, ChannelAccount.followers).where(
                ChannelAccount.business_id == bid, ChannelAccount.status.in_(("connected", "practice"))
            )
        )
    ).all()
    by_stage = dict(
        (
            await session.execute(
                select(Contact.stage, func.count())
                .where(Contact.business_id == bid, Contact.erased_at.is_(None))
                .group_by(Contact.stage)
            )
        )
        .tuples()
        .all()
    )
    at_least = {stage: sum(int(by_stage.get(s, 0)) for s in STAGES[i:]) for i, stage in enumerate(STAGES)}
    return {
        "followers": sum(int(f or 0) for _, f in accounts),
        "channels": [{"platform": p, "followers": int(f or 0)} for p, f in accounts],
        "contacts": sum(int(v) for v in by_stage.values()),
        "subscribers": at_least["subscriber"],
        "leads_total": at_least["lead"] - at_least["customer"],
        "customers": at_least["customer"],
        "leads_30d": await new_leads(session, bid, month_ago, now),
        "customers_30d": await new_customers(session, bid, month_ago, now),
        "revenue_30d_minor": await revenue(session, bid, month_ago, now),
        "churn_30d": await churn_rate(session, bid, month_ago, now),
        "review_rating": float(((business.settings or {}).get("gtm", {})).get("review_rating", 0.0)),
    }


def recommend(numbers: dict[str, Any], business: Business, *, launching: bool = False) -> dict[str, Any]:
    """The plan's decision table, in order of urgency."""
    followers, contacts = numbers["followers"], numbers["contacts"]
    leads, customers = numbers["leads_total"], numbers["customers"]
    rating = numbers.get("review_rating") or 0
    if 0 < rating < 3.8:
        goal = ("reputation", "review_rating", "Your review rating is low; fix trust before spending on growth.")
    elif launching:
        goal = ("awareness", "reach", "You're launching something new: build a launch campaign around it.")
    elif customers >= 10 and numbers["churn_30d"] > 0.05:
        goal = ("retention", "churn_rate", "Customers are leaving faster than 5% a month; keep them first.")
    elif followers < 500 and numbers["subscribers"] < 100:
        goal = ("awareness", "followers", "There's no audience yet: build awareness and a community.")
    elif contacts < 50:
        goal = (
            "demand",
            "new_leads" if business.business_model != "media" else "new_subscribers",
            "You have an audience but few contacts: capture them.",
        )
    elif leads >= 50 and customers <= max(1, int(leads * 0.02)):
        goal = ("sales", "new_customers", "Plenty of leads, few buyers: convert them.")
    elif customers >= 20 and numbers["churn_30d"] < 0.03:
        goal = ("partnerships", "partners_signed", "Happy customers but flat growth: upsell, referrals and partners.")
    else:
        goal = ("demand", "new_leads", "Grow the top of the funnel steadily.")
    goal_type, metric, why = goal
    approach = APPROACH_BY_MODEL.get(business.business_model, "sales_referrals")
    return {
        "goal": {"type": goal_type, "metric": metric, "why": why},
        "growth_approach": {
            "key": approach,
            "label": APPROACH_LABEL[approach],
            "why": f"Typical for {business.business_model} businesses; change it if yours differs.",
        },
        "alternatives": [
            g for g in ("awareness", "demand", "sales", "retention", "partnerships", "reputation") if g != goal_type
        ],
    }


async def diagnose(session: AsyncSession, business: Business, *, launching: bool = False) -> dict[str, Any]:
    numbers = await snapshot(session, business)
    return {"numbers": numbers, **recommend(numbers, business, launching=launching)}
