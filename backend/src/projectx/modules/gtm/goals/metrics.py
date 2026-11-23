"""Goal metrics, each measured from records (the funnel, payments, posts), never estimated."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Float, func, or_, select
from sqlalchemy import cast as sa_cast
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.modules.gtm.models import ChannelAccount, InboxItem, Post
from projectx.spine.tenancy.models import Business
from projectx.spine.twin.models import STAGES, Contact, ContactStageChange, Deal, Payment, Pipeline, Touch

Compute = Callable[[AsyncSession, uuid.UUID, datetime, datetime], Awaitable[float]]


@dataclass(frozen=True)
class MetricDef:
    key: str
    label: str
    goal_type: str
    unit: str  # count | currency_minor | percent | rating
    stage: str | None
    higher_is_better: bool
    stock: bool  # True: a level (followers), measured as current minus baseline; False: a flow in a window
    compute: Compute


def _stages_from(stage: str) -> list[str]:
    return list(STAGES[STAGES.index(stage) :])


async def _entered(session: AsyncSession, business_id: uuid.UUID, start: datetime, end: datetime, stage: str) -> float:
    """Distinct contacts that first reached `stage` (or beyond) inside the window."""
    later = _stages_from(stage)
    earlier = list(STAGES[: STAGES.index(stage)])
    created = select(Contact.id).where(
        Contact.business_id == business_id,
        Contact.created_at >= start,
        Contact.created_at < end,
        Contact.stage.in_(later),
    )
    crossed = select(ContactStageChange.contact_id).where(
        ContactStageChange.business_id == business_id,
        ContactStageChange.changed_at >= start,
        ContactStageChange.changed_at < end,
        ContactStageChange.to_stage.in_(later),
        or_(ContactStageChange.from_stage.is_(None), ContactStageChange.from_stage.in_(earlier)),
    )
    union = created.union(crossed).subquery()
    return float(await session.scalar(select(func.count()).select_from(union)) or 0)


async def new_subscribers(s: AsyncSession, b: uuid.UUID, start: datetime, end: datetime) -> float:
    return await _entered(s, b, start, end, "subscriber")


async def new_leads(s: AsyncSession, b: uuid.UUID, start: datetime, end: datetime) -> float:
    return await _entered(s, b, start, end, "lead")


async def new_customers(s: AsyncSession, b: uuid.UUID, start: datetime, end: datetime) -> float:
    return await _entered(s, b, start, end, "customer")


async def revenue(s: AsyncSession, b: uuid.UUID, start: datetime, end: datetime) -> float:
    total = await s.scalar(
        select(func.coalesce(func.sum(Payment.amount_minor), 0)).where(
            Payment.business_id == b, Payment.paid_at >= start, Payment.paid_at < end
        )
    )
    return float(total or 0)


async def followers(s: AsyncSession, b: uuid.UUID, start: datetime, end: datetime) -> float:
    total = await s.scalar(
        select(func.coalesce(func.sum(ChannelAccount.followers), 0)).where(
            ChannelAccount.business_id == b, ChannelAccount.status.in_(("connected", "practice"))
        )
    )
    return float(total or 0)


def _metric_number(key: str) -> Any:
    return func.coalesce(sa_cast(Post.metrics[key].astext, Float), 0.0)


async def reach(s: AsyncSession, b: uuid.UUID, start: datetime, end: datetime) -> float:
    total = await s.scalar(
        select(func.coalesce(func.sum(_metric_number("reach")), 0.0)).where(
            Post.business_id == b, Post.published_at >= start, Post.published_at < end
        )
    )
    return float(total or 0)


async def engagement_rate(s: AsyncSession, b: uuid.UUID, start: datetime, end: datetime) -> float:
    row = (
        await s.execute(
            select(
                func.coalesce(func.sum(_metric_number("engagements")), 0.0),
                func.coalesce(func.sum(_metric_number("reach")), 0.0),
            ).where(Post.business_id == b, Post.published_at >= start, Post.published_at < end)
        )
    ).one()
    return float(row[0]) / float(row[1]) if row[1] else 0.0


async def churn_rate(s: AsyncSession, b: uuid.UUID, start: datetime, end: datetime) -> float:
    base = await s.scalar(
        select(func.count())
        .select_from(Contact)
        .where(Contact.business_id == b, Contact.stage.in_(_stages_from("customer")), Contact.created_at < start)
    )
    churned = await s.scalar(
        select(func.count(func.distinct(ContactStageChange.contact_id))).where(
            ContactStageChange.business_id == b,
            ContactStageChange.to_side_state == "churned",
            ContactStageChange.changed_at >= start,
            ContactStageChange.changed_at < end,
        )
    )
    return round(float(churned or 0) / float(base), 4) if base else 0.0


async def upsell_revenue(s: AsyncSession, b: uuid.UUID, start: datetime, end: datetime) -> float:
    total = await s.scalar(
        select(func.coalesce(func.sum(Deal.value_minor), 0)).where(
            Deal.business_id == b,
            Deal.status == "won",
            Deal.won_at >= start,
            Deal.won_at < end,
            Deal.deal_type.in_(("upsell", "renewal")),
        )
    )
    return float(total or 0)


async def referrals(s: AsyncSession, b: uuid.UUID, start: datetime, end: datetime) -> float:
    return float(
        await s.scalar(
            select(func.count())
            .select_from(Touch)
            .where(
                Touch.business_id == b, Touch.kind == "referral", Touch.occurred_at >= start, Touch.occurred_at < end
            )
        )
        or 0
    )


async def partners_signed(s: AsyncSession, b: uuid.UUID, start: datetime, end: datetime) -> float:
    return float(
        await s.scalar(
            select(func.count())
            .select_from(Deal)
            .join(Pipeline, Pipeline.id == Deal.pipeline_id)
            .where(
                Deal.business_id == b,
                Pipeline.key == "partners",
                Deal.status == "won",
                Deal.won_at >= start,
                Deal.won_at < end,
            )
        )
        or 0
    )


async def mentions(s: AsyncSession, b: uuid.UUID, start: datetime, end: datetime) -> float:
    return float(
        await s.scalar(
            select(func.count())
            .select_from(InboxItem)
            .where(
                InboxItem.business_id == b,
                InboxItem.kind == "mention",
                InboxItem.received_at >= start,
                InboxItem.received_at < end,
            )
        )
        or 0
    )


async def review_rating(s: AsyncSession, b: uuid.UUID, start: datetime, end: datetime) -> float:
    business = await s.get(Business, b)
    return float(((business.settings or {}).get("gtm", {}) if business else {}).get("review_rating", 0.0))


async def negative_share(s: AsyncSession, b: uuid.UUID, start: datetime, end: datetime) -> float:
    row = (
        await s.execute(
            select(func.count(), func.count().filter(InboxItem.sentiment < -0.3)).where(
                InboxItem.business_id == b, InboxItem.received_at >= start, InboxItem.received_at < end
            )
        )
    ).one()
    return round(row[1] / row[0], 4) if row[0] else 0.0


METRICS: dict[str, MetricDef] = {
    m.key: m
    for m in (
        MetricDef("followers", "Followers", "awareness", "count", "audience", True, True, followers),
        MetricDef("reach", "Reach", "awareness", "count", "audience", True, False, reach),
        MetricDef(
            "engagement_rate", "Engagement rate", "awareness", "percent", "audience", True, False, engagement_rate
        ),
        MetricDef("new_subscribers", "New subscribers", "demand", "count", "subscriber", True, False, new_subscribers),
        MetricDef("new_leads", "New leads", "demand", "count", "lead", True, False, new_leads),
        MetricDef("new_customers", "New customers", "sales", "count", "customer", True, False, new_customers),
        MetricDef("revenue", "Revenue", "sales", "currency_minor", "customer", True, False, revenue),
        MetricDef("churn_rate", "Customer churn", "retention", "percent", "active", False, False, churn_rate),
        MetricDef(
            "upsell_revenue",
            "Upsell and renewal revenue",
            "retention",
            "currency_minor",
            "expansion",
            True,
            False,
            upsell_revenue,
        ),
        MetricDef("referrals", "Referrals", "retention", "count", "advocate", True, False, referrals),
        MetricDef("partners_signed", "Partners signed", "partnerships", "count", None, True, False, partners_signed),
        MetricDef("mentions", "Mentions", "reputation", "count", "audience", True, False, mentions),
        MetricDef("review_rating", "Review rating", "reputation", "rating", "audience", True, True, review_rating),
        MetricDef(
            "negative_share", "Share of negative messages", "reputation", "percent", None, False, False, negative_share
        ),
    )
}

DEFAULT_METRIC = {
    "awareness": "followers",
    "demand": "new_leads",
    "sales": "new_customers",
    "partnerships": "partners_signed",
    "retention": "churn_rate",
    "reputation": "review_rating",
}


async def measure(session: AsyncSession, business_id: uuid.UUID, key: str, start: datetime, end: datetime) -> float:
    return await METRICS[key].compute(session, business_id, start, end)


__all__ = ["DEFAULT_METRIC", "METRICS", "MetricDef", "measure"]
