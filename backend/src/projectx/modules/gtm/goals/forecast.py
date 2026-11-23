"""The feasibility forecast and budget planner.

Nobody can promise a goal is always hit, but the system can refuse unrealistic goals up front
and show what to expect instead ("with $200 and 30 days, expect 150-300 leads, not 1,000").
This is a driver-based Monte Carlo model computed entirely in code. Each driver (reach per post,
click rate, landing conversion, cost per lead…) is a range: learned from the business's own
history when there is enough of it, otherwise taken from the benchmark table below. The ranges
are starting assumptions to tune, and every forecast lists the assumptions it used.
"""

from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

SAMPLES = 2000

# (low, most likely, high) per driver. Reach is a share of followers per post.
REACH = {
    "instagram": (0.03, 0.08, 0.25),
    "tiktok": (0.05, 0.25, 2.0),
    "youtube": (0.03, 0.10, 0.6),
    "linkedin": (0.02, 0.06, 0.2),
    "x": (0.01, 0.04, 0.15),
    "threads": (0.02, 0.06, 0.2),
    "facebook": (0.01, 0.03, 0.1),
    "pinterest": (0.01, 0.05, 0.3),
    "telegram": (0.3, 0.5, 0.8),
    "discord": (0.1, 0.25, 0.5),
    "newsletter": (0.25, 0.4, 0.55),
    "blog": (0.0, 0.0, 0.0),
}
MIN_REACH = {
    "instagram": 40,
    "tiktok": 150,
    "youtube": 40,
    "linkedin": 40,
    "x": 25,
    "threads": 25,
    "facebook": 15,
    "pinterest": 25,
    "blog": 20,
}
CLICK_RATE = {
    "social": (0.002, 0.007, 0.02),
    "newsletter": (0.02, 0.04, 0.08),
    "telegram": (0.01, 0.03, 0.06),
    "discord": (0.005, 0.015, 0.04),
    "blog": (0.02, 0.05, 0.1),
}
LANDING_CONVERSION = {
    "newsletter": (0.10, 0.22, 0.40),
    "lead_magnet": (0.08, 0.20, 0.35),
    "consult": (0.01, 0.04, 0.08),
    "waitlist": (0.10, 0.25, 0.45),
    "webinar": (0.08, 0.18, 0.30),
    "media_kit": (0.03, 0.08, 0.15),
}
FOLLOW_RATE = (0.0005, 0.002, 0.008)  # new followers per person reached
PAID_CPL_USD = {"meta": (1.5, 4.0, 12.0), "google": (4.0, 12.0, 40.0), "linkedin": (20.0, 60.0, 150.0)}
PAID_CPM_USD = (3.0, 7.0, 15.0)
LEAD_TO_CUSTOMER = {"b2c": (0.005, 0.02, 0.06), "b2b": (0.03, 0.10, 0.25)}
PIPELINE_CONVERSION = (0.05, 0.15, 0.3)  # share of existing open leads that buy during the goal window
OUTREACH_REPLY = (0.03, 0.08, 0.15)
OUTREACH_CLOSE = (0.1, 0.25, 0.4)
CHURN_REDUCTION = (0.05, 0.15, 0.30)
RATING_GAIN_PER_MONTH = (0.02, 0.1, 0.3)
# Approximate USD rates used only to translate benchmark costs; override per business with gtm.fx_usd.
FX_FROM_USD = {
    "USD": 1.0,
    "INR": 85.0,
    "EUR": 0.92,
    "GBP": 0.79,
    "AED": 3.67,
    "SGD": 1.34,
    "AUD": 1.52,
    "CAD": 1.37,
    "JPY": 150.0,
}
DEFAULT_POSTS_PER_WEEK = {
    "instagram": 5,
    "tiktok": 7,
    "youtube": 1,
    "linkedin": 4,
    "x": 14,
    "threads": 7,
    "facebook": 4,
    "pinterest": 5,
    "telegram": 7,
    "discord": 5,
    "newsletter": 1,
    "blog": 1,
}


@dataclass
class ChannelInput:
    platform: str
    followers: int = 0
    posts_per_week: float | None = None


@dataclass
class ForecastInput:
    goal_type: str
    metric: str
    target: float
    baseline: float
    start: date
    deadline: date
    budget_minor: int = 0
    currency: str = "USD"
    channels: list[ChannelInput] = field(default_factory=list)
    paid_channels: list[str] = field(default_factory=list)
    page_kind: str = "lead_magnet"
    segment: str = "b2c"
    open_leads: int = 0
    customers: int = 0
    current_churn: float = 0.0
    avg_order_minor: int = 0
    outreach_per_week: int = 0
    fx_usd: float | None = None
    learned: dict[str, tuple[float, float, float]] = field(default_factory=dict)


@dataclass
class Forecast:
    p10: float
    p50: float
    p90: float
    probability: float
    verdict: str  # likely | stretch | unrealistic | achieved
    days: int
    daily_rate_p50: float
    breakdown: dict[str, float]
    assumptions: list[str]
    suggestions: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _tri(rng: random.Random, spec: tuple[float, float, float]) -> float:
    low, mode, high = spec
    if high <= low:
        return low
    return rng.triangular(low, high, mode)


def _percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    k = (len(sorted_values) - 1) * q
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return sorted_values[int(k)]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


def _fx(inp: ForecastInput) -> float:
    return inp.fx_usd or FX_FROM_USD.get(inp.currency.upper(), 1.0)


def _driver(inp: ForecastInput, name: str, default: tuple[float, float, float]) -> tuple[float, float, float]:
    return inp.learned.get(name, default)


def _organic_reach(rng: random.Random, inp: ForecastInput, weeks: float) -> dict[str, float]:
    out: dict[str, float] = {}
    for ch in inp.channels:
        if ch.platform in ("newsletter",):
            continue
        spec = _driver(inp, f"reach.{ch.platform}", REACH.get(ch.platform, (0.01, 0.04, 0.1)))
        per_post = max(ch.followers * _tri(rng, spec), MIN_REACH.get(ch.platform, 0))
        posts = (
            ch.posts_per_week if ch.posts_per_week is not None else DEFAULT_POSTS_PER_WEEK.get(ch.platform, 3)
        ) * weeks
        out[ch.platform] = per_post * posts
    return out


def _simulate_once(rng: random.Random, inp: ForecastInput, days: int) -> tuple[float, dict[str, float]]:
    weeks = days / 7
    budget_usd = inp.budget_minor / 100 / _fx(inp)
    parts: dict[str, float] = {}
    g, m = inp.goal_type, inp.metric

    if g == "awareness":
        reach = _organic_reach(rng, inp, weeks)
        paid_reach = (budget_usd / _tri(rng, PAID_CPM_USD) * 1000) if budget_usd else 0.0
        if m == "reach":
            parts = {**reach, "paid": paid_reach}
        else:
            follow = _tri(rng, _driver(inp, "follow_rate", FOLLOW_RATE))
            parts = {k: v * follow for k, v in reach.items()}
            parts["paid"] = paid_reach * follow * 0.5
        return sum(parts.values()), parts

    if g in ("demand", "sales"):
        reach = _organic_reach(rng, inp, weeks)
        conv = _tri(rng, _driver(inp, "landing_conversion", LANDING_CONVERSION.get(inp.page_kind, (0.05, 0.15, 0.3))))
        for platform, r in reach.items():
            ctr_key = platform if platform in CLICK_RATE else "social"
            parts[platform] = r * _tri(rng, _driver(inp, f"ctr.{platform}", CLICK_RATE[ctr_key])) * conv
        newsletter = next((c for c in inp.channels if c.platform == "newsletter"), None)
        if newsletter and m in ("new_customers", "revenue"):
            opens = newsletter.followers * _tri(rng, REACH["newsletter"]) * weeks * (newsletter.posts_per_week or 1)
            parts["newsletter"] = opens * _tri(rng, CLICK_RATE["newsletter"]) * conv * 0.3
        if budget_usd and inp.paid_channels:
            share = budget_usd / len(inp.paid_channels)
            for channel in inp.paid_channels:
                parts[f"paid_{channel}"] = share / _tri(
                    rng, _driver(inp, f"cpl.{channel}", PAID_CPL_USD.get(channel, (3, 8, 25)))
                )
        leads = sum(parts.values())
        if g == "demand":
            return leads, parts
        close = _tri(rng, _driver(inp, "lead_to_customer", LEAD_TO_CUSTOMER.get(inp.segment, LEAD_TO_CUSTOMER["b2c"])))
        customers = {k: v * close for k, v in parts.items()}
        customers["existing_pipeline"] = inp.open_leads * _tri(rng, PIPELINE_CONVERSION)
        if m == "revenue":
            value = inp.avg_order_minor or 0
            return sum(customers.values()) * value, {k: v * value for k, v in customers.items()}
        return sum(customers.values()), customers

    if g == "partnerships":
        outreach = (inp.outreach_per_week or 10) * weeks
        signed = outreach * _tri(rng, OUTREACH_REPLY) * _tri(rng, OUTREACH_CLOSE)
        return signed, {"outreach": signed}

    if g == "retention":
        if m == "churn_rate":
            reduced = inp.current_churn * (1 - _tri(rng, CHURN_REDUCTION))
            return reduced, {"churn": reduced}
        referrals = inp.customers * _tri(rng, (0.01, 0.04, 0.1)) * (days / 30)
        return referrals, {"customers": referrals}

    if g == "reputation":
        gained = _tri(rng, RATING_GAIN_PER_MONTH) * days / 30
        return min(inp.baseline + gained, 5.0) - inp.baseline, {"reviews": gained}

    return 0.0, {}


def forecast(inp: ForecastInput, *, seed: int = 7) -> Forecast:
    days = max((inp.deadline - inp.start).days, 1)
    rng = random.Random(seed)  # noqa: S311 - simulation, not security
    outcomes: list[float] = []
    totals: dict[str, float] = {}
    for _ in range(SAMPLES):
        value, parts = _simulate_once(rng, inp, days)
        outcomes.append(value)
        for k, v in parts.items():
            totals[k] = totals.get(k, 0.0) + v
    outcomes.sort()
    lower_is_better = inp.metric in ("churn_rate", "negative_share")
    # For level metrics (followers, rating) the model forecasts the change; the target is a level.
    needed = inp.target - inp.baseline if inp.metric in ("followers", "review_rating") else inp.target
    p10, p50, p90 = (_percentile(outcomes, q) for q in (0.1, 0.5, 0.9))
    if lower_is_better:
        probability = sum(1 for v in outcomes if v <= needed) / len(outcomes)
        verdict = "likely" if p50 <= needed else ("stretch" if p10 <= needed else "unrealistic")
    else:
        probability = sum(1 for v in outcomes if v >= needed) / len(outcomes)
        verdict = (
            "achieved"
            if needed <= 0
            else ("likely" if p50 >= needed else ("stretch" if p90 >= needed else "unrealistic"))
        )
    breakdown = {k: round(v / SAMPLES, 2) for k, v in sorted(totals.items(), key=lambda kv: -kv[1])}
    daily = p50 / days
    return Forecast(
        p10=round(p10, 2),
        p50=round(p50, 2),
        p90=round(p90, 2),
        probability=round(probability, 3),
        verdict=verdict,
        days=days,
        daily_rate_p50=round(daily, 3),
        breakdown=breakdown,
        assumptions=_assumptions(inp),
        suggestions=_suggestions(inp, verdict, needed, p50, daily, days, lower_is_better),
    )


def _assumptions(inp: ForecastInput) -> list[str]:
    notes = []
    if inp.learned:
        notes.append(f"Uses {len(inp.learned)} rates learned from this business's own results.")
    else:
        notes.append("No history yet: uses industry benchmark ranges, which will be replaced by your own results.")
    if inp.channels:
        notes.append("Channels: " + ", ".join(f"{c.platform} ({c.followers:,} followers)" for c in inp.channels))
    else:
        notes.append("No channels connected yet: organic reach assumes brand-new accounts.")
    if inp.budget_minor and inp.paid_channels:
        notes.append(f"Paid budget split across {', '.join(inp.paid_channels)}; costs per lead are benchmark ranges.")
    if inp.currency.upper() != "USD":
        notes.append(f"Benchmark costs converted at about {_fx(inp):g} {inp.currency.upper()} per USD.")
    return notes


def _suggestions(
    inp: ForecastInput, verdict: str, needed: float, p50: float, daily: float, days: int, lower_is_better: bool
) -> list[dict[str, Any]]:
    if verdict in ("likely", "achieved") or lower_is_better:
        return []
    out: list[dict[str, Any]] = []
    if daily > 0:
        extra_days = math.ceil(needed / daily) - days
        if extra_days > 0:
            out.append({"kind": "extend", "label": f"Give it {extra_days} more days", "days": extra_days})
    out.append(
        {"kind": "lower_target", "label": f"Aim for about {math.floor(p50):,} instead", "target": math.floor(p50)}
    )
    if inp.goal_type in ("demand", "sales") and inp.metric != "revenue":
        cpl_usd = PAID_CPL_USD["meta"][1]
        gap = max(needed - p50, 0)
        per_unit = (
            cpl_usd
            if inp.goal_type == "demand"
            else cpl_usd / LEAD_TO_CUSTOMER.get(inp.segment, LEAD_TO_CUSTOMER["b2c"])[1]
        )
        extra_budget_minor = int(gap * per_unit * _fx(inp) * 100)
        if extra_budget_minor > 0:
            out.append(
                {
                    "kind": "budget",
                    "label": "Add a paid lead campaign",
                    "budget_minor": extra_budget_minor,
                    "currency": inp.currency,
                }
            )
    out.append(
        {
            "kind": "switch_campaign",
            "label": "Try a different campaign",
            "playbook": "paid_lead_ads" if inp.goal_type == "demand" else "lead_magnet",
        }
    )
    return out
