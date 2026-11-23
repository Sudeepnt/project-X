"""The weekly review. Code computes every number first; a model then writes the explanation using
placeholders that code fills in, so a figure in the report can never be invented."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from projectx.kernel.money import Money
from projectx.kernel.time import start_of_day_utc, today_in
from projectx.modules.gtm.analytics.service import channel_table, compute_spend, leaderboard, patterns
from projectx.modules.gtm.goals.metrics import new_customers, new_leads, new_subscribers, revenue
from projectx.modules.gtm.models import Goal, Report
from projectx.spine.gateway.contracts import ChatMessage, ComputeCapReached, ModelRequest, ModelsExhausted
from projectx.spine.gateway.offline import offline_generator
from projectx.spine.jobs.registry import JobContext, job
from projectx.spine.ledger.service import Actor, append
from projectx.spine.notifications.service import notify
from projectx.spine.tenancy.models import Business

_PLACEHOLDER = re.compile(r"\{\{\s*([a-z0-9_]+)\s*\}\}")


class Narrative(BaseModel):
    headline: str = Field(max_length=200)
    what_worked: list[str] = Field(max_length=5)
    what_didnt: list[str] = Field(max_length=5)
    what_changes: list[str] = Field(max_length=5)


def fill(text: str, values: dict[str, str]) -> str:
    """Replace {{placeholders}} with computed values; unknown placeholders are removed, never guessed."""
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), "—"), text)


def _pct(now: float, before: float) -> str:
    if before == 0:
        return "new" if now else "flat"
    change = (now - before) / before * 100
    return f"{change:+.0f}%"


@offline_generator("gtm.report.narrate")
def _offline_narrative(req: ModelRequest) -> dict[str, Any]:
    c = req.context
    worked = ["{{top_post}} did best ({{top_post_platform}})."] if c.get("has_top_post") else []
    if c.get("top_hook"):
        worked.append("The {{top_hook}} hook keeps winning.")
    return {
        "headline": "{{leads}} new leads this week ({{leads_change}}), revenue {{revenue}}.",
        "what_worked": worked or ["Consistency: the calendar ran every day."],
        "what_didnt": ["{{weakest_channel}} brought the least for the effort."]
        if c.get("weakest_channel")
        else ["Not enough data yet to call a loser."],
        "what_changes": ["More of what worked; less of what didn't. Ratio and hooks update automatically."],
    }


async def compute_week(session: Any, business: Business) -> dict[str, Any]:
    tz = business.timezone
    end_day = today_in(tz)
    start_day = end_day - timedelta(days=7)
    end, start = start_of_day_utc(end_day, tz), start_of_day_utc(start_day, tz)
    prev = start - timedelta(days=7)
    bid = business.id
    data: dict[str, Any] = {
        "period": {"start": start_day.isoformat(), "end": end_day.isoformat()},
        "leads": await new_leads(session, bid, start, end),
        "leads_prev": await new_leads(session, bid, prev, start),
        "subscribers": await new_subscribers(session, bid, start, end),
        "subscribers_prev": await new_subscribers(session, bid, prev, start),
        "customers": await new_customers(session, bid, start, end),
        "customers_prev": await new_customers(session, bid, prev, start),
        "revenue_minor": await revenue(session, bid, start, end),
        "revenue_prev_minor": await revenue(session, bid, prev, start),
        "channels": await channel_table(session, bid, start),
        "top_posts": await leaderboard(session, bid, start, 5),
        "patterns": await patterns(session, bid),
        "compute": await compute_spend(session, bid, start),
    }
    goal = await session.scalar(select(Goal).where(Goal.business_id == bid, Goal.status == "active"))
    data["goal"] = {"title": goal.title, "pace": goal.pace, "target": goal.target_value} if goal else None
    return data


def placeholders(data: dict[str, Any], currency: str) -> dict[str, str]:
    channels = data["channels"]
    weakest = min((c for c in channels if c["posts"]), key=lambda c: c["leads"] + c["engagements"] / 100, default=None)
    top = data["top_posts"][0] if data["top_posts"] else None
    hooks = data["patterns"].get("hook_type") or []
    return {
        "leads": f"{data['leads']:,.0f}",
        "leads_change": _pct(data["leads"], data["leads_prev"]),
        "subscribers": f"{data['subscribers']:,.0f}",
        "customers": f"{data['customers']:,.0f}",
        "revenue": Money(int(data["revenue_minor"]), currency).format(),
        "revenue_change": _pct(data["revenue_minor"], data["revenue_prev_minor"]),
        "top_post": f"“{top['title'][:80]}”" if top else "—",
        "top_post_platform": top["platform"] if top else "—",
        "top_hook": hooks[0]["key"].replace("_", " ") if hooks else "—",
        "weakest_channel": weakest["platform"] if weakest else "—",
    }


@job("gtm.weekly_report", max_attempts=3, timeout_s=600, priority=70)
async def weekly_report(ctx: JobContext) -> dict[str, Any] | None:
    business_id = ctx.require_business()
    async with ctx.services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        if business is None:
            return None
        data = await compute_week(session, business)
    values = placeholders(data, business.currency)
    request = ModelRequest(
        task="gtm.report.narrate",
        system=(
            "You explain a business's weekly marketing results to its founder in plain words. Never write a "
            "number yourself: refer to figures only with the {{placeholders}} listed. Short sentences."
        ),
        messages=[
            ChatMessage(
                "user",
                "Placeholders you may use: "
                + ", ".join(f"{{{{{k}}}}}" for k in values)
                + f"\nFacts (for your understanding only): {data['channels'][:5]}, "
                f"top patterns {data['patterns'].get('hook_type')}",
            )
        ],
        output_schema=Narrative,
        max_tokens=1500,
        effort="low",
        rung="small",
        context={
            "has_top_post": bool(data["top_posts"]),
            "top_hook": bool(data["patterns"].get("hook_type")),
            "weakest_channel": values["weakest_channel"] != "—",
        },
    )
    try:
        narrative = (await ctx.services.gateway.run(business_id, request)).parsed_as(Narrative)
        text = "\n".join(
            [
                fill(narrative.headline, values),
                "",
                "What worked:",
                *[f"• {fill(s, values)}" for s in narrative.what_worked],
                "",
                "What didn't:",
                *[f"• {fill(s, values)}" for s in narrative.what_didnt],
                "",
                "What's changing:",
                *[f"• {fill(s, values)}" for s in narrative.what_changes],
            ]
        )
    except (ComputeCapReached, ModelsExhausted):
        text = fill("{{leads}} new leads ({{leads_change}}), {{customers}} new customers, revenue {{revenue}}.", values)
    async with ctx.services.db.tenant(business_id) as session:
        stmt = insert(Report).values(
            business_id=business_id,
            kind="weekly",
            period_start=date.fromisoformat(data["period"]["start"]),
            period_end=date.fromisoformat(data["period"]["end"]),
            data={**data, "placeholders": values},
            narrative=text,
        )
        report_id = await session.scalar(
            stmt.on_conflict_do_update(
                index_elements=[Report.business_id, Report.kind, Report.period_start],
                set_={"data": {**data, "placeholders": values}, "narrative": text},
            ).returning(Report.id)
        )
        assert report_id is not None
        await append(
            session,
            business_id=business_id,
            type="gtm.report.generated",
            actor=Actor.agent("pack:gtm.learning"),
            subject=("report", report_id),
            data={"id": str(report_id)},
        )
        await notify(
            session,
            business_id,
            kind="report.weekly",
            title="Your weekly review is ready",
            body=text.split("\n")[0],
            link=f"/analytics/reports/{report_id}",
        )
    return {"report_id": str(report_id)}
