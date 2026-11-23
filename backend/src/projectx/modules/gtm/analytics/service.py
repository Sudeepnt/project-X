"""Analytics, computed from records. Every number on a dashboard traces back to rows in the
ledger, the funnel, payments or post metrics; nothing is estimated by a model."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Date, Float, cast, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.time import start_of_day_utc, today_in
from projectx.modules.gtm.learning.patterns import top_patterns
from projectx.modules.gtm.models import Campaign, ContentItem, Post, TrackedLink
from projectx.spine.gateway.models import ModelCall
from projectx.spine.tenancy.models import Business
from projectx.spine.twin.models import STAGES, Contact, ContactStageChange, Payment, Touch


def _num(key: str) -> Any:
    return func.coalesce(cast(Post.metrics[key].astext, Float), 0.0)


async def daily_series(session: AsyncSession, business: Business, days: int = 30) -> list[dict[str, Any]]:
    tz = business.timezone
    end = today_in(tz)
    start = end - timedelta(days=days - 1)
    since = start_of_day_utc(start, tz)

    def local(col: Any) -> Any:
        return cast(func.timezone(tz, col), Date)

    bid = business.id
    contacts = dict(
        (
            await session.execute(
                select(local(Contact.created_at), func.count())
                .where(Contact.business_id == bid, Contact.created_at >= since)
                .group_by(text("1"))
            )
        )
        .tuples()
        .all()
    )
    customers = dict(
        (
            await session.execute(
                select(local(ContactStageChange.changed_at), func.count())
                .where(
                    ContactStageChange.business_id == bid,
                    ContactStageChange.changed_at >= since,
                    ContactStageChange.to_stage == "customer",
                )
                .group_by(text("1"))
            )
        )
        .tuples()
        .all()
    )
    revenue = dict(
        (
            await session.execute(
                select(local(Payment.paid_at), func.sum(Payment.amount_minor))
                .where(Payment.business_id == bid, Payment.paid_at >= since)
                .group_by(text("1"))
            )
        )
        .tuples()
        .all()
    )
    posts = dict(
        (
            await session.execute(
                select(local(Post.published_at), func.count())
                .where(Post.business_id == bid, Post.status == "published", Post.published_at >= since)
                .group_by(text("1"))
            )
        )
        .tuples()
        .all()
    )
    reach = dict(
        (
            await session.execute(
                select(local(Post.published_at), func.sum(_num("reach")))
                .where(Post.business_id == bid, Post.published_at >= since)
                .group_by(text("1"))
            )
        )
        .tuples()
        .all()
    )
    out = []
    for i in range(days):
        d: date = start + timedelta(days=i)
        out.append(
            {
                "date": d.isoformat(),
                "contacts": int(contacts.get(d, 0)),
                "customers": int(customers.get(d, 0)),
                "revenue_minor": int(revenue.get(d, 0) or 0),
                "posts": int(posts.get(d, 0)),
                "reach": int(reach.get(d, 0) or 0),
            }
        )
    return out


async def channel_table(session: AsyncSession, business_id: uuid.UUID, since: datetime) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(
                Post.platform,
                func.count(),
                func.sum(_num("reach")),
                func.sum(_num("engagements")),
                func.sum(_num("clicks")),
            )
            .where(Post.business_id == business_id, Post.status == "published", Post.published_at >= since)
            .group_by(Post.platform)
        )
    ).all()
    leads = dict(
        (
            await session.execute(
                select(Contact.first_touch["platform"].astext, func.count())
                .where(
                    Contact.business_id == business_id, Contact.created_at >= since, Contact.first_touch.is_not(None)
                )
                .group_by(text("1"))
            )
        )
        .tuples()
        .all()
    )
    customers = dict(
        (
            await session.execute(
                select(Contact.first_touch["platform"].astext, func.count())
                .where(
                    Contact.business_id == business_id,
                    Contact.stage.in_(STAGES[STAGES.index("customer") :]),
                    Contact.first_touch.is_not(None),
                )
                .group_by(text("1"))
            )
        )
        .tuples()
        .all()
    )
    out = []
    for platform, count, reach, engagements, clicks in rows:
        reach_f = float(reach or 0)
        out.append(
            {
                "platform": platform,
                "posts": int(count),
                "reach": int(reach_f),
                "engagements": int(engagements or 0),
                "engagement_rate": round(float(engagements or 0) / reach_f, 4) if reach_f else None,
                "clicks": int(clicks or 0),
                "leads": int(leads.get(platform, 0)),
                "customers": int(customers.get(platform, 0)),
            }
        )
    for platform, count in leads.items():
        if platform and not any(r["platform"] == platform for r in out):
            out.append(
                {
                    "platform": platform,
                    "posts": 0,
                    "reach": 0,
                    "engagements": 0,
                    "engagement_rate": None,
                    "clicks": 0,
                    "leads": int(count),
                    "customers": int(customers.get(platform, 0)),
                }
            )
    return sorted(out, key=lambda r: -(r["leads"] * 100 + r["reach"]))


async def attribution(session: AsyncSession, business_id: uuid.UUID, since: datetime) -> list[dict[str, Any]]:
    """Leads, customers and revenue per campaign, by first touch and by last touch."""
    campaigns = {
        c.id: c for c in (await session.execute(select(Campaign).where(Campaign.business_id == business_id))).scalars()
    }
    first = dict(
        (
            await session.execute(
                select(Contact.first_campaign_id, func.count())
                .where(
                    Contact.business_id == business_id,
                    Contact.created_at >= since,
                    Contact.first_campaign_id.is_not(None),
                )
                .group_by(Contact.first_campaign_id)
            )
        )
        .tuples()
        .all()
    )
    last = dict(
        (
            await session.execute(
                select(Contact.last_campaign_id, func.count())
                .where(
                    Contact.business_id == business_id,
                    Contact.created_at >= since,
                    Contact.last_campaign_id.is_not(None),
                )
                .group_by(Contact.last_campaign_id)
            )
        )
        .tuples()
        .all()
    )
    revenue = dict(
        (
            await session.execute(
                select(Payment.campaign_id, func.sum(Payment.amount_minor))
                .where(Payment.business_id == business_id, Payment.paid_at >= since, Payment.campaign_id.is_not(None))
                .group_by(Payment.campaign_id)
            )
        )
        .tuples()
        .all()
    )
    ids = set(first) | set(last) | set(revenue)
    return sorted(
        (
            {
                "campaign_id": str(cid),
                "name": campaigns[cid].name if cid in campaigns else "Unknown campaign",
                "first_touch_leads": int(first.get(cid, 0)),
                "last_touch_leads": int(last.get(cid, 0)),
                "revenue_minor": int(revenue.get(cid, 0) or 0),
            }
            for cid in ids
        ),
        key=lambda r: -(r["revenue_minor"] + r["first_touch_leads"]),
    )


async def leaderboard(
    session: AsyncSession, business_id: uuid.UUID, since: datetime, limit: int = 10
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(Post, ContentItem)
            .join(ContentItem, ContentItem.id == Post.content_id)
            .where(
                Post.business_id == business_id,
                Post.status == "published",
                Post.published_at >= since,
                Post.score.is_not(None),
            )
            .order_by(Post.score.desc())
            .limit(limit)
        )
    ).all()
    return [
        {
            "post_id": str(p.id),
            "content_id": str(c.id),
            "title": c.title,
            "platform": p.platform,
            "url": p.url,
            "score": p.score,
            "metrics": p.metrics,
            "hook_type": c.hook_type,
            "pillar": c.pillar,
            "intent": c.intent,
            "published_at": p.published_at.isoformat() if p.published_at else None,
        }
        for p, c in rows
    ]


async def link_stats(session: AsyncSession, business_id: uuid.UUID) -> dict[str, int]:
    total = await session.scalar(
        select(func.coalesce(func.sum(TrackedLink.clicks), 0)).where(TrackedLink.business_id == business_id)
    )
    return {"clicks": int(total or 0)}


async def compute_spend(session: AsyncSession, business_id: uuid.UUID, since: datetime) -> dict[str, Any]:
    row = (
        await session.execute(
            select(func.coalesce(func.sum(ModelCall.cost_micro_usd), 0), func.count()).where(
                ModelCall.business_id == business_id, ModelCall.created_at >= since
            )
        )
    ).one()
    return {"micro_usd": int(row[0]), "calls": int(row[1])}


async def patterns(session: AsyncSession, business_id: uuid.UUID) -> dict[str, list[dict[str, Any]]]:
    return {
        d: await top_patterns(session, business_id, d)
        for d in ("hook_type", "theme", "format", "hour", "platform", "intent", "playbook")
    }


async def referral_count(session: AsyncSession, business_id: uuid.UUID, since: datetime) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(Touch)
            .where(Touch.business_id == business_id, Touch.kind == "referral", Touch.occurred_at >= since)
        )
        or 0
    )
