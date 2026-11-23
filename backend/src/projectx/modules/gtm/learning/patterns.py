"""The memory of winning patterns. When a post's numbers have matured, its score updates the
running averages for its theme, hook, pillar, format, platform, hour, weekday and intent."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.time import utcnow, zone
from projectx.modules.gtm.models import ContentItem, ContentVariant, PatternStat, Post

ALPHA = 0.2  # EWMA weight of the newest observation


def post_score(metrics: dict[str, Any]) -> float | None:
    """0..1. Engagement rate (x10, capped) when reach is known, plus a click bonus."""
    reach = float(metrics.get("reach") or metrics.get("impressions") or 0)
    engagements = float(metrics.get("engagements") or 0)
    if reach <= 0:
        return None
    rate = engagements / reach
    clicks = float(metrics.get("clicks") or 0) / reach
    return round(min(1.0, rate * 10 + clicks * 20), 4)


async def bump(session: AsyncSession, business_id: uuid.UUID, dimension: str, key: str, score: float) -> None:
    stmt = insert(PatternStat).values(
        business_id=business_id,
        dimension=dimension,
        key=key[:80],
        n=1,
        sum_score=score,
        sum_sq=score * score,
        ewma=score,
        updated_at=utcnow(),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[PatternStat.business_id, PatternStat.dimension, PatternStat.key],
        set_={
            "n": PatternStat.n + 1,
            "sum_score": PatternStat.sum_score + score,
            "sum_sq": PatternStat.sum_sq + score * score,
            "ewma": PatternStat.ewma * (1 - ALPHA) + score * ALPHA,
            "updated_at": utcnow(),
        },
    )
    await session.execute(stmt)


async def learn_from_post(session: AsyncSession, post: Post, tz: str) -> float | None:
    score = post_score(post.metrics or {})
    if score is None or post.score is not None:
        return None
    post.score = score
    variant = await session.get(ContentVariant, post.variant_id)
    item = await session.get(ContentItem, post.content_id)
    if item is None:
        return score
    when = (post.published_at or utcnow()).astimezone(zone(tz))
    dims = {
        "pillar": item.pillar,
        "intent": item.intent,
        "platform": post.platform,
        "hour": str(when.hour),
        "weekday": str(when.weekday()),
    }
    if item.theme_id:
        dims["theme"] = str(item.theme_id)
    if item.hook_type:
        dims["hook_type"] = item.hook_type
    if variant is not None:
        dims["format"] = variant.format
    if item.campaign_id:
        from projectx.modules.gtm.models import Campaign

        campaign = await session.get(Campaign, item.campaign_id)
        if campaign is not None:
            dims["playbook"] = campaign.playbook_key
    for dimension, key in dims.items():
        await bump(session, post.business_id, dimension, str(key), score)
    return score


async def top_patterns(
    session: AsyncSession, business_id: uuid.UUID, dimension: str, limit: int = 5
) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(PatternStat)
                .where(PatternStat.business_id == business_id, PatternStat.dimension == dimension, PatternStat.n >= 3)
                .order_by(PatternStat.ewma.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [{"key": r.key, "n": r.n, "score": round(r.ewma, 3), "mean": round(r.sum_score / r.n, 3)} for r in rows]
