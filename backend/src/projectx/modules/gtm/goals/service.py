"""Goals: create with a feasibility forecast, activate (one main goal at a time), pace daily,
stop when hit or when a stop rule fires, and offer the owner options early when behind."""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.errors import Conflict, NotFound, ValidationFailed
from projectx.kernel.time import start_of_day_utc, today_in, utcnow
from projectx.modules.gtm.goals.diagnosis import snapshot
from projectx.modules.gtm.goals.forecast import ChannelInput, Forecast, ForecastInput, forecast
from projectx.modules.gtm.goals.metrics import METRICS, measure
from projectx.modules.gtm.models import Audience, Campaign, ChannelAccount, Goal
from projectx.spine.decisions.executors import ExecutionContext, ExecutionResult, executor
from projectx.spine.decisions.models import Decision
from projectx.spine.decisions.service import Proposal, propose
from projectx.spine.ledger.service import Actor, append
from projectx.spine.notifications.service import notify
from projectx.spine.tenancy.models import Business

PAGE_KIND_FOR = {
    "new_subscribers": "newsletter",
    "new_leads": "lead_magnet",
    "new_customers": "consult",
    "revenue": "consult",
}


async def build_forecast_input(
    session: AsyncSession,
    business: Business,
    *,
    goal_type: str,
    metric: str,
    target: float,
    deadline: date,
    budget_minor: int,
    start: date | None = None,
    paid_channels: list[str] | None = None,
    channels: list[str] | None = None,
) -> ForecastInput:
    if metric not in METRICS or METRICS[metric].goal_type != goal_type:
        raise ValidationFailed(f"{metric} is not a {goal_type} metric")
    numbers = await snapshot(session, business)
    accounts = (
        (
            await session.execute(
                select(ChannelAccount).where(
                    ChannelAccount.business_id == business.id, ChannelAccount.status.in_(("connected", "practice"))
                )
            )
        )
        .scalars()
        .all()
    )
    chans = [ChannelInput(a.platform, int(a.followers or 0)) for a in accounts]
    known = {c.platform for c in chans}
    for platform in channels or []:
        if platform not in known:
            chans.append(ChannelInput(platform, 0))
    if numbers["subscribers"] and "newsletter" not in known:
        chans.append(ChannelInput("newsletter", int(numbers["subscribers"])))
    audience = await session.scalar(
        select(Audience).where(Audience.business_id == business.id, Audience.is_primary.is_(True))
    )
    customers = int(numbers["customers"])
    avg_order = int(numbers["revenue_30d_minor"] / max(numbers["customers_30d"], 1)) if numbers["customers_30d"] else 0
    baseline = (
        numbers["followers"]
        if metric == "followers"
        else (numbers["review_rating"] if metric == "review_rating" else 0)
    )
    return ForecastInput(
        goal_type=goal_type,
        metric=metric,
        target=float(target),
        baseline=float(baseline),
        start=start or today_in(business.timezone),
        deadline=deadline,
        budget_minor=budget_minor,
        currency=business.currency,
        channels=chans,
        paid_channels=paid_channels or (["meta"] if budget_minor else []),
        page_kind=PAGE_KIND_FOR.get(metric, "lead_magnet"),
        segment=audience.segment if audience else "b2c",
        open_leads=int(numbers["leads_total"]),
        customers=customers,
        current_churn=float(numbers["churn_30d"]),
        avg_order_minor=avg_order,
        fx_usd=((business.settings or {}).get("gtm", {})).get("fx_usd"),
    )


async def create_goal(
    session: AsyncSession,
    business: Business,
    *,
    goal_type: str,
    metric: str,
    target: float,
    deadline: date,
    budget_minor: int,
    title: str | None,
    growth_approach: str,
    approval_mode: str,
    guardrail: dict[str, Any] | None,
    stop_rules: list[dict[str, Any]],
    channels: list[str] | None,
    paid_channels: list[str] | None,
    diagnosis: dict[str, Any] | None,
    ai_credit_budget_cents: int | None,
    actor: Actor,
) -> tuple[Goal, Forecast]:
    today = today_in(business.timezone)
    if deadline <= today:
        raise ValidationFailed("The deadline must be in the future", extra={"field": "deadline"})
    if (deadline - today).days > 366:
        raise ValidationFailed("Keep goals within a year; set a new one when this is done", extra={"field": "deadline"})
    inp = await build_forecast_input(
        session,
        business,
        goal_type=goal_type,
        metric=metric,
        target=target,
        deadline=deadline,
        budget_minor=budget_minor,
        paid_channels=paid_channels,
        channels=channels,
    )
    result = forecast(inp)
    definition = METRICS[metric]
    goal = Goal(
        business_id=business.id,
        type=goal_type,
        metric=metric,
        unit=definition.unit,
        stage=definition.stage,
        title=(title or f"{definition.label}: {target:,.0f} by {deadline:%d %b}")[:200],
        baseline_value=inp.baseline,
        target_value=target,
        current_value=inp.baseline if definition.stock else 0,
        start_date=today,
        deadline=deadline,
        budget_minor=budget_minor,
        currency=business.currency,
        ai_credit_budget_cents=ai_credit_budget_cents,
        status="draft",
        growth_approach=growth_approach,
        approval_mode=approval_mode,
        guardrail=guardrail,
        stop_rules=stop_rules or default_stop_rules(goal_type),
        forecast={**result.as_dict(), "computed_at": utcnow().isoformat()},
        diagnosis=diagnosis,
    )
    session.add(goal)
    await session.flush()
    await append(
        session,
        business_id=business.id,
        type="gtm.goal.created",
        actor=actor,
        subject=("goal", goal.id),
        data={"goal_id": str(goal.id), "type": goal_type, "status": "draft", "target": float(target)},
    )
    return goal, result


def default_stop_rules(goal_type: str) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = [
        {"kind": "goal_hit", "action": "maintenance"},
        {"kind": "deadline", "action": "stop_and_report"},
        {"kind": "budget_used", "action": "stop_and_report"},
    ]
    if goal_type in ("demand", "sales"):
        rules += [
            {"kind": "zero_results_after_spend", "spend_share": 0.3, "action": "switch_creative"},
            {"kind": "cpl_over_target", "multiple": 2.0, "days": 3, "action": "switch_creative"},
        ]
    return rules


async def activate_goal(
    session: AsyncSession, business: Business, goal: Goal, *, actor: Actor, replace: bool = False
) -> Goal:
    if goal.status == "active":
        return goal
    if goal.status not in ("draft", "paused"):
        raise Conflict(f"A {goal.status} goal can't be restarted; set a new goal instead")
    current = await session.scalar(select(Goal).where(Goal.business_id == business.id, Goal.status == "active"))
    if current is not None and current.id != goal.id:
        if not replace:
            raise Conflict(
                "Only one main goal runs at a time. Stop the current goal first, or replace it.",
                extra={"active_goal_id": str(current.id)},
            )
        await end_goal(session, business, current, status="stopped", reason="replaced by a new goal", actor=actor)
    if goal.status == "draft":
        goal.start_date = today_in(business.timezone)
        if METRICS[goal.metric].stock:
            now = utcnow()
            goal.baseline_value = await measure(session, business.id, goal.metric, now, now)
            goal.current_value = goal.baseline_value
    goal.status = "active"
    await session.flush()
    await append(
        session,
        business_id=business.id,
        type="gtm.goal.changed",
        actor=actor,
        subject=("goal", goal.id),
        data={"goal_id": str(goal.id), "type": goal.type, "status": "active", "target": goal.target_value},
    )
    return goal


async def end_goal(
    session: AsyncSession, business: Business, goal: Goal, *, status: str, reason: str, actor: Actor
) -> Goal:
    goal.status, goal.ended_at, goal.end_reason = status, utcnow(), reason[:300]
    campaign_status = "completed" if status in ("achieved", "maintenance") else "stopped"
    for campaign in (
        await session.execute(select(Campaign).where(Campaign.goal_id == goal.id, Campaign.status == "active"))
    ).scalars():
        campaign.status, campaign.stop_reason = campaign_status, reason[:300]
    await append(
        session,
        business_id=business.id,
        type="gtm.goal.changed",
        actor=actor,
        subject=("goal", goal.id),
        data={"goal_id": str(goal.id), "type": goal.type, "status": status, "reason": reason[:300]},
    )
    return goal


def _expected_to_date(goal: Goal, elapsed: float, total: float) -> float:
    """A gentle ramp (marketing compounds): elapsed^1.15 rather than a straight line."""
    share = min(max(elapsed / total, 0.0), 1.0) ** 1.15 if total > 0 else 1.0
    if METRICS[goal.metric].stock:
        return goal.baseline_value + (goal.target_value - goal.baseline_value) * share
    return goal.target_value * share


async def pace_goal(session: AsyncSession, business: Business, goal: Goal) -> dict[str, Any]:
    """The daily check: actual vs expected, projected finish, status, and stop rules."""
    definition = METRICS[goal.metric]
    tz = business.timezone
    now = utcnow()
    start = start_of_day_utc(goal.start_date, tz)
    actual = await measure(session, business.id, goal.metric, start, now)
    total_days = max((goal.deadline - goal.start_date).days, 1)
    elapsed_days = max((now - start).total_seconds() / 86400, 0.0)
    remaining = max(total_days - elapsed_days, 0.0)
    week_ago = max(start, now - timedelta(days=7))
    recent = await measure(session, business.id, goal.metric, week_ago, now)
    window = max((now - week_ago).total_seconds() / 86400, 1.0)
    if definition.stock:
        progress = actual - goal.baseline_value
        rate = progress / max(elapsed_days, 1.0)
        projected = actual + rate * remaining
    elif goal.metric in ("churn_rate", "negative_share", "engagement_rate"):
        projected = actual
    else:
        rate = (recent / window) * 0.6 + (actual / max(elapsed_days, 1.0)) * 0.4
        projected = actual + rate * remaining
    expected = _expected_to_date(goal, elapsed_days, total_days)
    target = goal.target_value
    lower_better = not definition.higher_is_better
    if (actual <= target) if lower_better else (actual >= target):
        status = "achieved"
    elif lower_better:
        status = "on_track" if projected <= target else ("behind" if projected <= target * 1.3 else "at_risk")
    elif projected >= target * 1.1:
        status = "ahead"
    elif projected >= target * 0.9:
        status = "on_track"
    elif projected >= target * 0.7:
        status = "behind"
    else:
        status = "at_risk"
    pace = {
        "actual": round(actual, 2),
        "expected_to_date": round(expected, 2),
        "projected": round(projected, 2),
        "status": status,
        "days_left": math.ceil(remaining),
        "computed_at": now.isoformat(),
    }
    goal.current_value = actual
    goal.pace = pace
    await append(
        session,
        business_id=business.id,
        type="gtm.goal.paced",
        actor=Actor.agent("pack:gtm.goals"),
        subject=("goal", goal.id),
        data={
            "goal_id": str(goal.id),
            "actual": round(actual, 4),
            "expected_to_date": round(expected, 4),
            "projected": round(projected, 4),
            "status": status,
        },
    )
    await _apply_stop_rules(session, business, goal, pace)
    return pace


async def _apply_stop_rules(session: AsyncSession, business: Business, goal: Goal, pace: dict[str, Any]) -> None:
    agent = Actor.agent("pack:gtm.goals")
    if pace["status"] == "achieved":
        await end_goal(session, business, goal, status="achieved", reason="goal hit", actor=agent)
        await notify(
            session,
            business.id,
            kind="goal.achieved",
            level="success",
            title=f"Goal hit: {goal.title}",
            body="Campaigns moved to maintenance. Set the next goal when you're ready.",
            link=f"/goals/{goal.id}",
        )
        return
    if today_in(business.timezone) > goal.deadline:
        await end_goal(session, business, goal, status="missed", reason="deadline passed", actor=agent)
        await notify(
            session,
            business.id,
            kind="goal.missed",
            level="warning",
            title=f"Deadline passed: {goal.title}",
            body=f"Reached {pace['actual']:,.0f} of {goal.target_value:,.0f}. The weekly report explains why.",
            link=f"/goals/{goal.id}",
        )
        return
    if goal.budget_minor and goal.spent_minor >= goal.budget_minor:
        await end_goal(session, business, goal, status="stopped", reason="budget used up", actor=agent)
        await notify(
            session,
            business.id,
            kind="goal.budget",
            level="warning",
            title=f"Budget used up: {goal.title}",
            link=f"/goals/{goal.id}",
        )
        return
    if pace["status"] in ("behind", "at_risk"):
        await _offer_options(session, business, goal, pace)


async def _offer_options(session: AsyncSession, business: Business, goal: Goal, pace: dict[str, Any]) -> None:
    """Tell the owner early and offer options: more time, more budget, a lower target or a different campaign."""
    recent = await session.scalar(
        select(Decision.id).where(
            Decision.business_id == business.id,
            Decision.action_type == "gtm.adjust_goal",
            Decision.subject_id == goal.id,
            Decision.created_at > utcnow() - timedelta(days=4),
        )
    )
    if recent is not None:
        return
    projected, target = pace["projected"], goal.target_value
    elapsed = max((today_in(business.timezone) - goal.start_date).days, 1)
    rate = max(pace["actual"] / elapsed, 0.0001)
    extra_days = max(math.ceil((target - pace["actual"]) / rate) - pace["days_left"], 1)
    options = [
        {
            "key": "extend",
            "label": f"Give it {extra_days} more days",
            "description": "Same plan, later deadline.",
            "payload": {"kind": "extend", "days": extra_days},
        },
        {
            "key": "lower_target",
            "label": f"Lower the target to {math.floor(projected):,}",
            "description": "Keep the deadline; aim for what the current pace delivers.",
            "payload": {"kind": "lower_target", "target": math.floor(projected)},
        },
        {
            "key": "switch_campaign",
            "label": "Try a different campaign",
            "description": "Keep the goal, change the approach.",
            "payload": {"kind": "switch_campaign"},
        },
    ]
    if goal.forecast and goal.type in ("demand", "sales"):
        options.insert(
            1,
            {
                "key": "budget",
                "label": "Add budget for paid lead ads",
                "description": "Put money behind what already works organically.",
                "payload": {"kind": "budget", "extra_minor": max(goal.budget_minor // 2, 5000)},
            },
        )
    await propose(
        session,
        business.id,
        Proposal(
            action_type="gtm.adjust_goal",
            title=f"{goal.title} is {pace['status'].replace('_', ' ')}",
            summary=f"At this pace you'll reach about {projected:,.0f} of {target:,.0f} by {goal.deadline:%d %b}.",
            why_now="The earlier you adjust, the cheaper it is.",
            if_no="The plan continues as it is.",
            options=options,
            subject=("goal", goal.id),
            requested_by="pack:gtm.goals",
            priority=10,
            forecast={"metric": goal.metric, "expected": projected, "target": target},
        ),
    )


@executor("gtm.adjust_goal")
async def _adjust_goal(ctx: ExecutionContext) -> ExecutionResult:
    async with ctx.services.db.tenant(ctx.business_id) as session:
        decision = await session.get(Decision, ctx.decision_id)
        goal = await session.get(Goal, decision.subject_id) if decision and decision.subject_id else None
        if decision is None or goal is None:
            raise NotFound("Goal not found")
        option = next((o for o in (decision.options or []) if o.get("key") == ctx.option), None)
        payload = (option or {}).get("payload", {})
        kind = payload.get("kind")
        before = {"deadline": goal.deadline.isoformat(), "target": goal.target_value, "budget": goal.budget_minor}
        if kind == "extend":
            goal.deadline = goal.deadline + timedelta(days=int(payload["days"]))
        elif kind == "lower_target":
            goal.target_value = float(payload["target"])
        elif kind == "budget":
            goal.budget_minor = goal.budget_minor + int(payload["extra_minor"])
        await append(
            session,
            business_id=ctx.business_id,
            type="gtm.goal.changed",
            actor=Actor.agent("pack:gtm.goals"),
            subject=("goal", goal.id),
            data={
                "goal_id": str(goal.id),
                "type": goal.type,
                "status": goal.status,
                "target": goal.target_value,
                "reason": f"owner chose {kind}",
            },
        )
    return ExecutionResult(
        data={"kind": kind, "before": before}, summary=f"Goal adjusted: {kind}", undo_payload={"before": before}
    )


async def pace_all(session: AsyncSession, business: Business) -> list[dict[str, Any]]:
    goals = (
        (await session.execute(select(Goal).where(Goal.business_id == business.id, Goal.status == "active")))
        .scalars()
        .all()
    )
    return [await pace_goal(session, business, goal) for goal in goals]


def goal_view(g: Goal) -> dict[str, Any]:
    definition = METRICS.get(g.metric)
    return {
        "id": str(g.id),
        "type": g.type,
        "title": g.title,
        "metric": g.metric,
        "metric_label": definition.label if definition else g.metric,
        "unit": g.unit,
        "stage": g.stage,
        "baseline_value": g.baseline_value,
        "target_value": g.target_value,
        "current_value": g.current_value,
        "start_date": g.start_date.isoformat(),
        "deadline": g.deadline.isoformat(),
        "budget_minor": g.budget_minor,
        "spent_minor": g.spent_minor,
        "currency": g.currency,
        "ai_credit_budget_cents": g.ai_credit_budget_cents,
        "status": g.status,
        "growth_approach": g.growth_approach,
        "approval_mode": g.approval_mode,
        "guardrail": g.guardrail,
        "stop_rules": g.stop_rules,
        "forecast": g.forecast,
        "pace": g.pace,
        "diagnosis": g.diagnosis,
        "ended_at": g.ended_at.isoformat() if g.ended_at else None,
        "end_reason": g.end_reason,
        "higher_is_better": definition.higher_is_better if definition else True,
        "created_at": g.created_at.isoformat(),
    }
