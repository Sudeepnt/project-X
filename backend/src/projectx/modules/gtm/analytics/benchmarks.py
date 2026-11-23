"""Cross-business benchmarks: opt-in, anonymous, and only shown when enough businesses contribute.

Privacy by construction:
  * Off by default. Only businesses that share can see the benchmarks (a fair exchange).
  * Contributions carry no business id: a salted one-way hash (HMAC with the server secret) lets a
    business update or withdraw its own rows, and nothing else.
  * Only rates and averages are shared, never content, names or contacts.
  * A figure appears only when at least MIN_CONTRIBUTORS businesses of the same kind contributed it.
  * Turning sharing off deletes everything this business ever contributed.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import date, timedelta
from statistics import median, quantiles
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.api.deps import AdminDep, BusinessDep
from projectx.kernel.time import today_in, utcnow
from projectx.modules.gtm.analytics.service import channel_table
from projectx.modules.gtm.config import gtm_settings, update_gtm_settings
from projectx.modules.gtm.models import AdMetricDaily, BenchmarkContribution, LandingPage
from projectx.services import Services
from projectx.spine.jobs.registry import JobContext, job
from projectx.spine.ledger.service import Actor, append
from projectx.spine.tenancy.models import Business

MIN_CONTRIBUTORS = 5
WEEKS = 8
router = APIRouter(prefix="/api/v1/b/{business_id}/gtm/analytics/benchmarks", tags=["gtm: analytics"])


def contributor_id(secret: str, business_id: uuid.UUID) -> str:
    return hmac.new(secret.encode(), b"benchmarks:" + business_id.bytes, hashlib.sha256).hexdigest()


def sharing(business: Business) -> bool:
    return bool(gtm_settings(business).get("benchmarks", {}).get("share", False))


async def own_figures(session: AsyncSession, business: Business, days: int = 7) -> list[tuple[str, str, float]]:
    """(metric, platform, value) for the last `days` days. Rates and averages only."""
    since = utcnow() - timedelta(days=days)
    out: list[tuple[str, str, float]] = []
    for row in await channel_table(session, business.id, since):
        if row["posts"] <= 0:
            continue
        out.append(("engagement_rate", row["platform"], float(row["engagement_rate"])))
        out.append(("reach_per_post", row["platform"], row["reach"] / row["posts"]))
        out.append(("posts_per_week", row["platform"], row["posts"] * 7 / days))
    pages = (
        await session.execute(
            select(func.sum(LandingPage.views), func.sum(LandingPage.submissions)).where(
                LandingPage.business_id == business.id
            )
        )
    ).one()
    if pages[0] and pages[0] >= 100:
        out.append(("page_conversion", "", float(pages[1] or 0) / float(pages[0])))
    ads = (
        await session.execute(
            select(func.sum(AdMetricDaily.spend_minor), func.sum(AdMetricDaily.leads)).where(
                AdMetricDaily.business_id == business.id, AdMetricDaily.day >= since.date()
            )
        )
    ).one()
    if ads[1] and business.currency == "USD":  # cost figures only compare within one currency
        out.append(("cost_per_lead_usd", "", float(ads[0] or 0) / 100 / float(ads[1])))
    return out


@job("gtm.benchmarks_contribute", max_attempts=4, timeout_s=120, priority=80)
async def contribute(ctx: JobContext) -> dict[str, Any]:
    business_id = ctx.require_business()
    secret = ctx.services.settings.secret_key.get_secret_value()
    who = contributor_id(secret, business_id)
    async with ctx.services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        assert business is not None
        if not sharing(business):
            return {"shared": 0}
        week = today_in(business.timezone) - timedelta(days=today_in(business.timezone).weekday())
        figures = await own_figures(session, business)
        for metric, platform, value in figures:
            await session.execute(
                insert(BenchmarkContribution)
                .values(
                    contributor=who,
                    week=week,
                    business_model=business.business_model,
                    metric=metric,
                    platform=platform,
                    value=value,
                )
                .on_conflict_do_update(
                    index_elements=["contributor", "week", "metric", "platform"],
                    set_={"value": value, "business_model": business.business_model},
                )
            )
    return {"shared": len(figures)}


async def withdraw(services: Services, session: AsyncSession, business_id: uuid.UUID) -> int:
    who = contributor_id(services.settings.secret_key.get_secret_value(), business_id)
    result = await session.execute(delete(BenchmarkContribution).where(BenchmarkContribution.contributor == who))
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def benchmarks_for(
    session: AsyncSession, business_model: str, since: date
) -> dict[tuple[str, str], dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(BenchmarkContribution).where(
                    BenchmarkContribution.business_model == business_model, BenchmarkContribution.week >= since
                )
            )
        )
        .scalars()
        .all()
    )
    # One value per contributor per metric: their most recent week.
    latest: dict[tuple[str, str], dict[str, tuple[date, float]]] = {}
    for r in rows:
        slot = latest.setdefault((r.metric, r.platform), {})
        if r.contributor not in slot or slot[r.contributor][0] < r.week:
            slot[r.contributor] = (r.week, r.value)
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for key, by_contributor in latest.items():
        values = sorted(v for _, v in by_contributor.values())
        if len(values) < MIN_CONTRIBUTORS:
            continue
        q1, _, q3 = quantiles(values, n=4)
        out[key] = {"median": median(values), "p25": q1, "p75": q3, "businesses": len(values)}
    return out


@router.get("")
async def view(ctx: BusinessDep) -> dict[str, Any]:
    shares = sharing(ctx.business)
    if not shares:
        return {"sharing": False, "min_businesses": MIN_CONTRIBUTORS, "items": []}
    since = today_in(ctx.business.timezone) - timedelta(weeks=WEEKS)
    bench = await benchmarks_for(ctx.session, ctx.business.business_model, since)
    mine = {(m, p): v for m, p, v in await own_figures(ctx.session, ctx.business, days=28)}
    items = [
        {"metric": m, "platform": p or None, "you": mine.get((m, p)), **stats}
        for (m, p), stats in sorted(bench.items())
    ]
    return {
        "sharing": True,
        "min_businesses": MIN_CONTRIBUTORS,
        "business_model": ctx.business.business_model,
        "items": items,
    }


class ShareIn(BaseModel):
    share: bool


@router.put("")
async def set_sharing(body: ShareIn, ctx: AdminDep) -> dict[str, Any]:
    update_gtm_settings(ctx.business, {"benchmarks": {"share": body.share}})
    removed = 0
    if body.share:
        from projectx.spine.jobs.queue import enqueue

        await enqueue(ctx.session, "gtm.benchmarks_contribute", business_id=ctx.business_id)
    else:
        removed = await withdraw(ctx.services, ctx.session, ctx.business_id)
    await append(
        ctx.session,
        business_id=ctx.business_id,
        type="spine.policy.updated",
        actor=Actor.user(ctx.user.id),
        data={"fields": ["gtm.benchmarks.share"]},
    )
    return {"sharing": body.share, "removed": removed}
