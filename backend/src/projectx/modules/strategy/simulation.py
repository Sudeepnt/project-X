"""The simulation: everything the company knows, run forward a few thousand times, so "what if" has an answer.

How it works (all numbers from code; a model only turns a question into settings and never touches the maths):

1. Calibrate from the business's own records, department by department:
   - Sales (GTM): deals won in the last 90 days → how many a month (Poisson) and how big (lognormal fit); open deals
     close with their stage's probability around their expected close date.
   - Subscriptions (Finance): monthly recurring revenue from active subscriptions; cancellation rate from history.
   - Collections (Finance): what share of invoiced money gets paid, and how late.
   - Costs: payroll (People), software (Admin), everything else from the books (Finance), with its monthly wobble.
   - Cash today and money already owed to the business.
   Anything the records can't tell (a brand-new business) uses a stated default, and the result says so.
2. Apply the scenario: price change, hires, marketing spend (with diminishing returns), cost cuts, a cash injection,
   better or worse win rate or churn.
3. Run 2,000 futures month by month (fixed seed: the same question gives the same answer), then report percentiles
   (p10 / p50 / p90) per month and the probability of hitting each goal, scenario next to baseline.

This is a base to tune: every assumption is listed with where it came from, so the parameters can be argued with.
"""

from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.time import today_in
from projectx.spine.tenancy.models import Business
from projectx.spine.twin.models import Deal, Pipeline

RUNS = 2000
MAX_MONTHS = 36


# ---------------------------------------------------------------- what the business looks like today


@dataclass
class Assumption:
    name: str
    value: Any
    source: str  # "your data: …" or "default: …"


@dataclass
class Baseline:
    currency: str
    cash_minor: int
    receivable_minor: int
    deals_per_month: float
    deal_log_mean: float
    deal_log_sd: float
    pipeline: list[tuple[int, float, int]]  # (value, probability, month index it is expected to close)
    mrr_minor: int
    arpu_minor: int  # average monthly revenue per subscription
    new_subs_per_month: float
    churn_monthly: float
    collection_rate: float
    collection_delay_months: int
    payroll_monthly_minor: int
    software_monthly_minor: int
    other_costs_monthly_minor: int
    other_costs_sd_minor: int
    assumptions: list[Assumption] = field(default_factory=list)


async def calibrate(session: AsyncSession, business: Business) -> Baseline:
    from projectx.modules.finance import books
    from projectx.modules.finance.models import Invoice
    from projectx.modules.finance.payments import CustomerSubscription, Plan
    from projectx.modules.legal.models import Asset
    from projectx.modules.people.models import Employee

    today = today_in(business.timezone)
    since = today - timedelta(days=90)
    a: list[Assumption] = []

    # Sales
    won = [
        int(v)
        for v in (
            await session.execute(
                select(Deal.value_minor).where(Deal.status == "won", Deal.won_at >= since, Deal.value_minor > 0)
            )
        ).scalars()
    ]
    if won:
        rate = len(won) / 3
        logs = [math.log(v) for v in won]
        mean = sum(logs) / len(logs)
        sd = math.sqrt(sum((x - mean) ** 2 for x in logs) / max(len(logs) - 1, 1)) if len(logs) > 1 else 0.5
        a.append(Assumption("New deals a month", round(rate, 2), f"your data: {len(won)} won in the last 90 days"))
        a.append(Assumption("Typical deal", int(math.exp(mean)), f"your data: median of {len(won)} won deals"))
    else:
        rate, mean, sd = 0.5, math.log(50_000), 0.6
        a.append(Assumption("New deals a month", rate, "default: no deals won in the last 90 days"))
        a.append(Assumption("Typical deal", 50_000, "default: no won deals to learn from"))
    sd = max(min(sd, 1.5), 0.2)

    pipeline: list[tuple[int, float, int]] = []
    rows = (
        await session.execute(
            select(Deal, Pipeline).join(Pipeline, Pipeline.id == Deal.pipeline_id).where(Deal.status == "open")
        )
    ).all()
    for deal, pipe in rows:
        stage = next((s for s in pipe.stages if s.get("key") == deal.stage_key), None)
        prob = float(stage.get("probability", 0.1)) if stage else 0.1
        month = max(0, ((deal.expected_close or today + timedelta(days=45)) - today).days // 30)
        if deal.value_minor > 0:
            pipeline.append((deal.value_minor, prob, min(month, MAX_MONTHS - 1)))
    a.append(Assumption("Open deals in the pipeline", len(pipeline), "your data: open deals and their stage odds"))

    # Subscriptions
    active = (
        await session.execute(
            select(Plan.amount_minor, Plan.interval)
            .join(CustomerSubscription, CustomerSubscription.plan_id == Plan.id)
            .where(CustomerSubscription.status.in_(("active", "trialing", "past_due")))
        )
    ).all()
    mrr = sum(amount if interval == "month" else amount // 12 for amount, interval in active)
    arpu = mrr // len(active) if active else 0
    new_subs = await session.scalar(
        select(func.count()).select_from(CustomerSubscription).where(CustomerSubscription.created_at >= since)
    )
    cancelled = await session.scalar(
        select(func.count())
        .select_from(CustomerSubscription)
        .where(CustomerSubscription.status == "canceled", CustomerSubscription.updated_at >= since)
    )
    # Beta(1, 20) prior: about 5% a month until the business's own history says otherwise.
    exposure = max(len(active) + int(cancelled or 0), 0) * 3
    churn = (1 + int(cancelled or 0)) / (21 + exposure)
    a.append(Assumption("Monthly recurring revenue", mrr, f"your data: {len(active)} active subscription(s)"))
    a.append(
        Assumption(
            "Monthly cancellations",
            round(churn, 3),
            f"{'your data' if exposure else 'default'}: {cancelled or 0} cancelled in 90 days",
        )
    )

    # Collections
    issued = (
        await session.execute(
            select(Invoice.total_minor, Invoice.paid_minor, Invoice.status).where(
                Invoice.issue_date.is_not(None), Invoice.issue_date < today - timedelta(days=30)
            )
        )
    ).all()
    billed = sum(t for t, _, s in issued if s != "void")
    collected = sum(p for _, p, s in issued if s != "void")
    collection = collected / billed if billed else 0.95
    a.append(
        Assumption(
            "Share of invoiced money collected",
            round(collection, 2),
            "your data" if billed else "default: no invoice history yet",
        )
    )
    receivable = int(
        await session.scalar(
            select(func.coalesce(func.sum(Invoice.total_minor - Invoice.paid_minor), 0)).where(
                Invoice.status.in_(("sent", "partially_paid", "overdue"))
            )
        )
        or 0
    )

    # Costs
    payroll = int(
        await session.scalar(
            select(func.coalesce(func.sum(Employee.monthly_salary_minor), 0)).where(
                Employee.status.in_(("active", "onboarding", "leaving"))
            )
        )
        or 0
    )
    software = 0
    for cost, billing in (
        await session.execute(select(Asset.cost_minor, Asset.billing).where(Asset.status == "active"))
    ).all():
        software += cost if billing == "monthly" else cost // 12 if billing == "yearly" else 0
    months: list[int] = []
    for back in range(1, 4):
        end = (today.replace(day=1) - timedelta(days=30 * (back - 1))).replace(day=1)
        start = (end - timedelta(days=1)).replace(day=1)
        pnl = await books.profit_and_loss(session, start, end)
        other = pnl["total_expenses_minor"] - (await books.balances(session, start=start, end=end)).get(
            books.SALARIES, 0
        )
        other -= (await books.balances(session, start=start, end=end)).get(books.SOFTWARE, 0)
        months.append(max(other, 0))
    other_mean = sum(months) // 3
    other_sd = int(math.sqrt(sum((m - other_mean) ** 2 for m in months) / 3))
    a.append(Assumption("Payroll a month", payroll, "your data: People"))
    a.append(Assumption("Software a month", software, "your data: Admin assets"))
    a.append(Assumption("Other costs a month", other_mean, "your data: the books, last 3 months"))
    cash = (await books.balances(session)).get(books.CASH, 0)
    a.append(Assumption("Cash today", cash, "your data: the books"))

    return Baseline(
        currency=business.currency,
        cash_minor=cash,
        receivable_minor=receivable,
        deals_per_month=rate,
        deal_log_mean=mean,
        deal_log_sd=sd,
        pipeline=pipeline,
        mrr_minor=mrr,
        arpu_minor=arpu,
        new_subs_per_month=int(new_subs or 0) / 3,
        churn_monthly=churn,
        collection_rate=max(0.3, min(collection, 1.0)),
        collection_delay_months=1,
        payroll_monthly_minor=payroll,
        software_monthly_minor=software,
        other_costs_monthly_minor=other_mean,
        other_costs_sd_minor=other_sd,
        assumptions=a,
    )


# ---------------------------------------------------------------- the question


class Hire(BaseModel):
    monthly_cost_minor: int = Field(ge=0)
    start_month: int = Field(default=0, ge=0, le=MAX_MONTHS)
    adds_deals_per_month: float = Field(
        default=0.0, ge=0, le=20, description="for a salesperson: extra deals a month once ramped"
    )


class Scenario(BaseModel):
    price_change_pct: float = Field(default=0.0, ge=-90, le=500)
    deal_flow_change_pct: float = Field(default=0.0, ge=-100, le=1000, description="more or fewer deals won")
    churn_change_pct: float = Field(default=0.0, ge=-100, le=1000)
    hires: list[Hire] = Field(default_factory=list, max_length=50)
    marketing_monthly_minor: int = Field(default=0, ge=0)
    cost_cut_monthly_minor: int = Field(default=0, ge=0)
    cash_injection_minor: int = Field(default=0, ge=0, description="a raise or a loan, arriving in month 0")


class Goal(BaseModel):
    kind: Literal["revenue_total", "monthly_revenue", "mrr", "cash_floor"]
    target_minor: int = Field(description="cash_floor: the least cash you'll accept; others: the target")
    by_month: int = Field(ge=1, le=MAX_MONTHS, description="months from now")
    label: str = ""


class Question(BaseModel):
    months: int = Field(default=12, ge=1, le=MAX_MONTHS)
    scenario: Scenario = Field(default_factory=Scenario)
    goals: list[Goal] = Field(default_factory=list, max_length=10)
    # How many extra deals a month each 100,000 minor units of monthly marketing buys, at the start of the curve.
    # Unknown for most businesses: a stated assumption to tune, with square-root diminishing returns.
    marketing_deals_per_100k: float = Field(default=0.5, ge=0, le=50)


# ---------------------------------------------------------------- the engine


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    if lam > 30:  # normal approximation for big rates
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _run_once(b: Baseline, q: Question, rng: random.Random) -> dict[str, list[int]]:
    s = q.scenario
    price = 1 + s.price_change_pct / 100
    churn = min(max(b.churn_monthly * (1 + s.churn_change_pct / 100), 0.0), 1.0)
    # Higher prices usually mean fewer deals: a mild default elasticity of -0.5 (10% dearer → 5% fewer deals).
    elasticity = 1 - 0.5 * (price - 1) if price >= 1 else 1 + 0.3 * (1 - price)
    base_rate = max(b.deals_per_month * (1 + s.deal_flow_change_pct / 100) * max(elasticity, 0.05), 0.0)
    marketing_rate = (
        q.marketing_deals_per_100k * math.sqrt(s.marketing_monthly_minor / 100_000)
        if s.marketing_monthly_minor
        else 0.0
    )
    cash = b.cash_minor + s.cash_injection_minor
    subs = round(b.mrr_minor / b.arpu_minor) if b.arpu_minor else 0
    arpu = round(b.arpu_minor * price)
    incoming = [0] * (q.months + b.collection_delay_months + 1)
    incoming[0] += round(b.receivable_minor * b.collection_rate)
    pipeline_by_month: dict[int, list[tuple[int, float]]] = {}
    for value, prob, m in b.pipeline:
        pipeline_by_month.setdefault(m, []).append((value, prob))
    out: dict[str, list[int]] = {"revenue": [], "cash": [], "mrr": [], "costs": []}
    for m in range(q.months):
        hires_rate = sum(
            h.adds_deals_per_month * min(1.0, (m - h.start_month) / 3) for h in s.hires if m >= h.start_month
        )
        deals = _poisson(rng, base_rate + marketing_rate + hires_rate)
        one_off = sum(round(rng.lognormvariate(b.deal_log_mean, b.deal_log_sd) * price) for _ in range(deals))
        for value, prob in pipeline_by_month.get(m, []):
            if rng.random() < prob:
                one_off += value  # already-agreed prices: a price change doesn't touch open deals
        lost = sum(1 for _ in range(subs) if rng.random() < churn) if subs < 2000 else round(subs * churn)
        subs = max(subs - lost + _poisson(rng, b.new_subs_per_month * max(elasticity, 0.05)), 0)
        mrr = subs * arpu
        revenue = one_off + mrr
        # Money arrives when customers pay: some late, some never.
        paid = sum(1 for _ in range(10) if rng.random() < b.collection_rate) / 10
        incoming[m + b.collection_delay_months] += round(one_off * paid)
        incoming[m] += mrr  # card subscriptions are collected as they renew
        costs = (
            b.payroll_monthly_minor
            + sum(h.monthly_cost_minor for h in s.hires if m >= h.start_month)
            + b.software_monthly_minor
            + max(0, round(rng.gauss(b.other_costs_monthly_minor, b.other_costs_sd_minor or 1)))
            + s.marketing_monthly_minor
            - s.cost_cut_monthly_minor
        )
        cash += incoming[m] - max(costs, 0)
        out["revenue"].append(revenue)
        out["cash"].append(cash)
        out["mrr"].append(mrr)
        out["costs"].append(max(costs, 0))
    return out


def _pct(values: list[int], p: float) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(p * (len(ordered) - 1))))]


def _hit(goal: Goal, run: dict[str, list[int]]) -> bool:
    n = goal.by_month
    if goal.kind == "revenue_total":
        return sum(run["revenue"][:n]) >= goal.target_minor
    if goal.kind == "monthly_revenue":
        return run["revenue"][n - 1] >= goal.target_minor
    if goal.kind == "mrr":
        return run["mrr"][n - 1] >= goal.target_minor
    return min(run["cash"][:n]) >= goal.target_minor  # cash_floor


def simulate(b: Baseline, q: Question, *, seed: int = 7) -> dict[str, Any]:
    rng = random.Random(seed)  # noqa: S311  (statistics, not secrets: a fixed seed makes answers repeatable)
    runs = [_run_once(b, q, rng) for _ in range(RUNS)]
    months = []
    for m in range(q.months):
        row: dict[str, Any] = {"month": m + 1}
        for key in ("revenue", "cash", "mrr", "costs"):
            values = [r[key][m] for r in runs]
            row[key] = {"p10": _pct(values, 0.1), "p50": _pct(values, 0.5), "p90": _pct(values, 0.9)}
        months.append(row)
    goals = []
    for g in q.goals:
        if g.by_month > q.months:
            continue
        hits = sum(1 for r in runs if _hit(g, r))
        goals.append({**g.model_dump(), "probability": round(hits / RUNS, 3)})
    out_of_cash = [next((i + 1 for i, c in enumerate(r["cash"]) if c < 0), None) for r in runs]
    broke = [m for m in out_of_cash if m is not None]
    totals = [sum(r["revenue"]) for r in runs]
    return {
        "months": months,
        "goals": goals,
        "revenue_total": {"p10": _pct(totals, 0.1), "p50": _pct(totals, 0.5), "p90": _pct(totals, 0.9)},
        "chance_cash_runs_out": round(len(broke) / RUNS, 3),
        "cash_runs_out_month_p50": _pct(broke, 0.5) if broke else None,
        "runs": RUNS,
    }


def compare(b: Baseline, q: Question) -> dict[str, Any]:
    """The scenario next to doing nothing, same random futures for both (common random numbers)."""
    baseline_q = q.model_copy(update={"scenario": Scenario()})
    base = simulate(b, baseline_q)
    scen = simulate(b, q)
    delta = {
        "revenue_total_p50_minor": scen["revenue_total"]["p50"] - base["revenue_total"]["p50"],
        "cash_end_p50_minor": scen["months"][-1]["cash"]["p50"] - base["months"][-1]["cash"]["p50"],
        "chance_cash_runs_out": round(scen["chance_cash_runs_out"] - base["chance_cash_runs_out"], 3),
        "goals": [
            {"label": g["label"] or g["kind"], "scenario": g["probability"], "baseline": bg["probability"]}
            for g, bg in zip(scen["goals"], base["goals"], strict=True)
        ],
    }
    return {
        "currency": b.currency,
        "assumptions": [asdict(x) for x in b.assumptions],
        "question": q.model_dump(),
        "baseline": base,
        "scenario": scen,
        "difference": delta,
    }


def explain(result: dict[str, Any]) -> str:
    """A plain summary built by code from the numbers, most important first."""
    from projectx.kernel.money import format_money

    cur = result["currency"]
    s, d = result["scenario"], result["difference"]
    lines = []
    for g in d["goals"]:
        lines.append(
            f"{g['label']}: {round(g['scenario'] * 100)}% likely (doing nothing: {round(g['baseline'] * 100)}%)."
        )
    lines.append(
        f"Revenue over the period: most likely {format_money(s['revenue_total']['p50'], cur)} "
        f"(range {format_money(s['revenue_total']['p10'], cur)} to {format_money(s['revenue_total']['p90'], cur)})."
    )
    if s["chance_cash_runs_out"] > 0:
        when = f", most likely in month {s['cash_runs_out_month_p50']}" if s["cash_runs_out_month_p50"] else ""
        lines.append(f"Chance cash runs out: {round(s['chance_cash_runs_out'] * 100)}%{when}.")
    else:
        lines.append("Cash stays above zero in every simulated future.")
    if d["revenue_total_p50_minor"]:
        sign = "more" if d["revenue_total_p50_minor"] > 0 else "less"
        lines.append(
            f"Compared with doing nothing: {format_money(abs(d['revenue_total_p50_minor']), cur)} {sign} revenue."
        )
    return " ".join(lines)


__all__ = ["Goal", "Question", "Scenario", "calibrate", "compare", "explain", "simulate"]
