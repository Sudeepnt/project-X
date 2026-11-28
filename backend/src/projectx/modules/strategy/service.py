"""Strategy and steering: one view of the whole company, and risks caught before they hurt.

Every department reports its numbers (`Module.numbers`); this module reads them all, checks plain rules across
departments (cash against payroll, runway, late replies, unsigned contracts…), tracks objectives against the
same numbers, and once a week writes a steering review. A new risk is announced on the bus
(`strategy.risk.raised`) so any department can react, and owners are told. All numbers come from code.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.money import format_money
from projectx.kernel.time import today_in
from projectx.modules.strategy.models import Objective, Review
from projectx.services import Services
from projectx.spine.ledger.events import EventData, register_event
from projectx.spine.ledger.service import Actor, append
from projectx.spine.modules import modules
from projectx.spine.notifications.service import notify
from projectx.spine.tenancy.models import Business

AGENT = Actor.agent("strategy")


class RiskRaised(EventData):
    risk: str
    severity: str
    module: str


class ObjectiveChanged(EventData):
    objective_id: str
    status: str
    metric: str


register_event("strategy.risk.raised", "The steering review found a risk", RiskRaised)
register_event("strategy.objective.changed", "An objective was met or missed", ObjectiveChanged)


@dataclass(frozen=True)
class Risk:
    key: str
    severity: str  # high | medium | low
    module: str
    message: str


async def company_numbers(session: AsyncSession, business_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for m in modules():
        if m.numbers is not None and m.key != "strategy":
            try:
                out[m.key] = await m.numbers(session, business_id)
            except Exception:  # one department's report never blanks the whole view
                out[m.key] = {"error": "unavailable"}
    return out


def find_risks(n: dict[str, dict[str, Any]]) -> list[Risk]:
    """Cross-department rules, in plain code. Each is explainable in one sentence."""
    risks: list[Risk] = []
    fin, people, ops, legal = n.get("finance", {}), n.get("people", {}), n.get("ops", {}), n.get("legal", {})
    cur = fin.get("currency", "USD")
    months = fin.get("runway_months")
    if months is not None and months < 6:
        risks.append(
            Risk(
                "runway",
                "high" if months < 3 else "medium",
                "finance",
                f"Cash lasts about {months} months at this burn.",
            )
        )
    cash, payroll = fin.get("cash_minor"), people.get("monthly_payroll_minor", 0)
    if cash is not None and payroll and cash < payroll:
        risks.append(
            Risk(
                "payroll_cover",
                "high",
                "finance",
                f"Cash ({format_money(cash, cur)}) is below one month of payroll ({format_money(payroll, cur)}).",
            )
        )
    overdue = fin.get("receivables_overdue_minor", 0)
    if overdue and overdue > max(fin.get("revenue_mtd_minor", 0), 1) // 2:
        risks.append(
            Risk("receivables", "medium", "finance", f"{format_money(overdue, cur)} is overdue from customers.")
        )
    if ops.get("tickets_past_sla"):
        risks.append(
            Risk("sla", "medium", "ops", f"{ops['tickets_past_sla']} customer(s) waiting past the reply deadline.")
        )
    if ops.get("overdue_tasks"):
        risks.append(Risk("delivery", "medium", "ops", f"{ops['overdue_tasks']} delivery task(s) are late."))
    if legal.get("unsigned_contracts"):
        risks.append(Risk("unsigned", "low", "legal", f"{legal['unsigned_contracts']} contract(s) are still unsigned."))
    if legal.get("open_access_tasks", 0) >= 5:
        risks.append(Risk("access", "medium", "legal", f"{legal['open_access_tasks']} account changes are waiting."))
    return risks


def metric_value(n: dict[str, dict[str, Any]], metric: str) -> float | None:
    module, _, key = metric.partition(".")
    value = n.get(module, {}).get(key)
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def objective_state(o: Objective, value: float | None, today: date) -> tuple[str, float | None]:
    """(status, progress 0..1). Met as soon as the target is reached; missed only after the due date."""
    if value is None:
        return o.status, None
    met = value >= o.target if o.direction == "up" else value <= o.target
    if met:
        return "met", 1.0
    if today > o.due_date:
        return "missed", None
    start = o.start_value if o.start_value is not None else 0.0
    span = (o.target - start) if o.direction == "up" else (start - o.target)
    done = (value - start) if o.direction == "up" else (start - value)
    return "active", max(0.0, min(1.0, done / span)) if span else None


async def overview(session: AsyncSession, business: Business) -> dict[str, Any]:
    n = await company_numbers(session, business.id)
    today = today_in(business.timezone)
    objectives = []
    for o in (await session.execute(select(Objective).where(Objective.status == "active"))).scalars():
        value = metric_value(n, o.metric)
        status, progress = objective_state(o, value, today)
        objectives.append({**objective_view(o), "value": value, "progress": progress, "would_be": status})
    return {
        "numbers": n,
        "risks": [r.__dict__ for r in find_risks(n)],
        "objectives": objectives,
    }


async def weekly(services: Services, business_id: uuid.UUID) -> None:
    async with services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        if business is None:
            return
        today = today_in(business.timezone)
        week = f"{today.isocalendar().year}-W{today.isocalendar().week:02d}"
        if await session.scalar(select(Review.id).where(Review.week == week)):
            return
        n = await company_numbers(session, business_id)
        risks = find_risks(n)
        previous = await session.scalar(select(Review).order_by(Review.created_at.desc()).limit(1))
        known = {r["key"] for r in (previous.risks if previous else [])}
        obj_lines = []
        for o in (
            await session.execute(select(Objective).where(Objective.status == "active").with_for_update())
        ).scalars():
            value = metric_value(n, o.metric)
            if o.start_value is None and value is not None:
                o.start_value = value
            o.last_value = value
            status, progress = objective_state(o, value, today)
            if status != o.status:
                o.status = status
                await append(
                    session,
                    business_id=business_id,
                    type="strategy.objective.changed",
                    actor=AGENT,
                    subject=("objective", o.id),
                    data={"objective_id": str(o.id), "status": status, "metric": o.metric},
                )
            obj_lines.append({"id": str(o.id), "title": o.title, "status": o.status, "progress": progress})
        for r in risks:
            if r.key not in known:
                await append(
                    session,
                    business_id=business_id,
                    type="strategy.risk.raised",
                    actor=AGENT,
                    data={"risk": r.key, "severity": r.severity, "module": r.module},
                )
        new = [r for r in risks if r.key not in known]
        summary = (
            "No risks this week."
            if not risks
            else f"{len(risks)} risk(s), {len(new)} new. Most urgent: {sorted(risks, key=_rank)[0].message}"
        )
        session.add(
            Review(
                business_id=business_id,
                week=week,
                numbers=n,
                risks=[r.__dict__ for r in risks],
                objectives=obj_lines,
                summary=summary,
            )
        )
        if new:
            await notify(
                session,
                business_id,
                kind="strategy.review",
                level="warning" if any(r.severity == "high" for r in new) else "info",
                title=f"Weekly review: {summary}"[:200],
                body="\n".join(r.message for r in sorted(risks, key=_rank)),
                link="/strategy",
            )


def _rank(r: Risk) -> int:
    return {"high": 0, "medium": 1, "low": 2}[r.severity]


def objective_view(o: Objective) -> dict[str, Any]:
    return {
        "id": str(o.id),
        "title": o.title,
        "metric": o.metric,
        "target": o.target,
        "direction": o.direction,
        "due_date": o.due_date.isoformat(),
        "start_value": o.start_value,
        "last_value": o.last_value,
        "status": o.status,
    }


def review_view(r: Review) -> dict[str, Any]:
    return {"week": r.week, "summary": r.summary, "risks": r.risks, "objectives": r.objectives, "numbers": r.numbers}
