"""Dashboards, weekly reports, and the opt-in public build-in-public dashboard."""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import select

from projectx.api.deps import AdminDep, BusinessDep, EditorDep
from projectx.kernel.errors import NotFound
from projectx.kernel.money import Money
from projectx.kernel.time import utcnow
from projectx.modules.gtm.analytics.service import (
    attribution,
    channel_table,
    compute_spend,
    daily_series,
    leaderboard,
    link_stats,
    patterns,
    referral_count,
)
from projectx.modules.gtm.config import gtm_settings, update_gtm_settings
from projectx.modules.gtm.cycles import numbers
from projectx.modules.gtm.models import Report
from projectx.services import Services
from projectx.spine.jobs.queue import enqueue
from projectx.spine.tenancy.models import Business

router = APIRouter(prefix="/api/v1/b/{business_id}/gtm/analytics", tags=["gtm: analytics"])
public_router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[3] / "templates"))


@router.get("/overview")
async def overview(ctx: BusinessDep, days: int = 30) -> dict[str, Any]:
    days = max(7, min(days, 120))
    since = utcnow() - timedelta(days=days)
    return {
        "numbers": await numbers(ctx.session, ctx.business_id),
        "series": await daily_series(ctx.session, ctx.business, days),
        "channels": await channel_table(ctx.session, ctx.business_id, since),
        "links": await link_stats(ctx.session, ctx.business_id),
        "referrals": await referral_count(ctx.session, ctx.business_id, since),
        "compute": await compute_spend(ctx.session, ctx.business_id, since),
    }


@router.get("/attribution")
async def attribution_report(ctx: BusinessDep, days: int = 90) -> dict[str, Any]:
    return {"campaigns": await attribution(ctx.session, ctx.business_id, utcnow() - timedelta(days=min(days, 365)))}


@router.get("/content")
async def content_report(ctx: BusinessDep, days: int = 30) -> dict[str, Any]:
    since = utcnow() - timedelta(days=min(days, 365))
    return {
        "leaderboard": await leaderboard(ctx.session, ctx.business_id, since, 20),
        "patterns": await patterns(ctx.session, ctx.business_id),
    }


@router.get("/reports")
async def reports(ctx: BusinessDep) -> dict[str, Any]:
    rows = (
        (
            await ctx.session.execute(
                select(Report)
                .where(Report.business_id == ctx.business_id)
                .order_by(Report.period_start.desc())
                .limit(52)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": str(r.id),
                "kind": r.kind,
                "period_start": r.period_start.isoformat(),
                "period_end": r.period_end.isoformat(),
                "headline": r.narrative.split("\n")[0],
            }
            for r in rows
        ]
    }


@router.get("/reports/{report_id}")
async def report(report_id: uuid.UUID, ctx: BusinessDep) -> dict[str, Any]:
    r = await ctx.session.get(Report, report_id)
    if r is None:
        raise NotFound("Report not found")
    return {
        "id": str(r.id),
        "kind": r.kind,
        "period_start": r.period_start.isoformat(),
        "period_end": r.period_end.isoformat(),
        "data": r.data,
        "narrative": r.narrative,
    }


@router.post("/reports/run", status_code=status.HTTP_202_ACCEPTED)
async def run_report(ctx: EditorDep) -> dict[str, Any]:
    job_id = await enqueue(ctx.session, "gtm.weekly_report", business_id=ctx.business_id)
    return {"job_id": str(job_id) if job_id else None}


class PublicDashboardIn(BaseModel):
    enabled: bool
    metrics: list[str] = Field(default_factory=lambda: ["revenue", "subscribers", "leads", "customers"])


PUBLIC_METRICS = {"revenue", "subscribers", "leads", "customers", "churn", "compute_cost", "posts"}


@router.put("/public-dashboard")
async def set_public_dashboard(body: PublicDashboardIn, ctx: AdminDep) -> dict[str, Any]:
    chosen = [m for m in body.metrics if m in PUBLIC_METRICS]
    update_gtm_settings(ctx.business, {"public_dashboard": {"enabled": body.enabled, "metrics": chosen}})
    return {
        "enabled": body.enabled,
        "metrics": chosen,
        "url": f"{ctx.services.settings.base_url}/public/{ctx.business.slug}/dashboard",
    }


async def _public_numbers(services: Services, business: Business, chosen: list[str]) -> list[dict[str, str]]:
    async with services.db.tenant(business.id) as session:
        n = await numbers(session, business.id)
        spend = await compute_spend(session, business.id, utcnow() - timedelta(days=30))
        series = await daily_series(session, business, 30)
    values = {
        "revenue": ("Revenue, last 30 days", n["revenue"]["display"]),
        "subscribers": ("New subscribers, 30 days", f"{n['new_subscribers_30d']:,}"),
        "leads": ("New leads, 30 days", f"{n['new_leads_30d']:,}"),
        "customers": ("Customers", f"{n['customers']:,}"),
        "churn": ("Customer churn, 30 days", f"{n['churn']['rate'] * 100:.1f}%"),
        "compute_cost": ("AI cost, 30 days", Money(round(spend["micro_usd"] / 10_000), "USD").format()),
        "posts": ("Posts published, 30 days", f"{sum(d['posts'] for d in series):,}"),
    }
    return [{"key": k, "label": values[k][0], "value": values[k][1]} for k in chosen if k in values]


@public_router.get("/public/{business_slug}/dashboard")
async def public_dashboard(business_slug: str, request: Request) -> Response:
    services: Services = request.app.state.services
    async with services.db.session() as session:
        business = await session.scalar(
            select(Business).where(Business.slug == business_slug, Business.status == "active")
        )
    settings = gtm_settings(business)["public_dashboard"] if business else {}
    if business is None or not settings.get("enabled"):
        return HTMLResponse("<h1>Not found</h1>", status_code=404)
    tiles = await _public_numbers(services, business, list(settings.get("metrics", [])))
    if request.headers.get("accept", "").startswith("application/json"):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            {"business": business.name, "metrics": tiles, "as_of": utcnow().isoformat()},
            headers={"Cache-Control": "public, max-age=300"},
        )
    response = templates.TemplateResponse(
        request,
        "public/dashboard.html",
        {"business": business, "tiles": tiles, "primary": "#0071e3", "as_of": utcnow().strftime("%d %b %Y, %H:%M UTC")},
    )
    response.headers["Cache-Control"] = "public, max-age=300"
    return response
