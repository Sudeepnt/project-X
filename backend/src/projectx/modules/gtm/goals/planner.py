"""Budget planner: what a goal is likely to reach at different budgets and on different paid channels.

Runs the same Monte Carlo forecast the goal uses, so the planner and the goal never disagree. Paid results
have diminishing returns, so the planner can show where an extra unit of budget stops paying for itself.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

from projectx.modules.gtm.goals.forecast import ForecastInput, forecast

LEVELS = (0.0, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0)
PAID = ("meta", "google", "linkedin")


@dataclass
class Level:
    budget_minor: int
    p10: float
    p50: float
    p90: float
    probability: float
    cost_per_extra_minor: int | None  # what each extra result costs compared with the level below


@dataclass
class ChannelOption:
    channel: str
    p10: float
    p50: float
    p90: float
    cost_per_result_minor: int | None


def paid_mean(breakdown: dict[str, float]) -> float:
    return sum(v for k, v in breakdown.items() if k.startswith("paid"))


def plan_budget(inp: ForecastInput, *, budget_minor: int, channels: list[str] | None = None) -> dict[str, Any]:
    chosen = [c for c in (channels or ["meta", "google"]) if c in PAID] or ["meta"]
    levels: list[Level] = []
    paid_before = 0.0
    for factor in LEVELS:
        spend = int(budget_minor * factor)
        f = forecast(replace(inp, budget_minor=spend, paid_channels=chosen if spend else []))
        # Marginal cost from the paid contribution's mean, not the noisy overall median.
        paid = paid_mean(f.breakdown)
        extra: int | None = None
        if levels and spend > levels[-1].budget_minor:
            gained = paid - paid_before
            extra = round((spend - levels[-1].budget_minor) / gained) if gained > 0 else None
        paid_before = paid
        levels.append(Level(spend, f.p10, f.p50, f.p90, f.probability, extra))
    organic = levels[0].p50
    options: list[ChannelOption] = []
    for channel in chosen:
        f = forecast(replace(inp, budget_minor=budget_minor, paid_channels=[channel]))
        gained = paid_mean(f.breakdown)
        options.append(
            ChannelOption(channel, f.p10, f.p50, f.p90, round(budget_minor / gained) if gained > 0 else None)
        )
    options.sort(key=lambda o: o.p50, reverse=True)
    # The largest budget where an extra result still costs at most twice what the first paid results did.
    base = next((lv.cost_per_extra_minor for lv in levels if lv.cost_per_extra_minor), None)
    recommended = 0
    for lv in levels:
        if base and lv.cost_per_extra_minor and lv.cost_per_extra_minor <= 2 * base:
            recommended = lv.budget_minor
    return {
        "levels": [asdict(lv) for lv in levels],
        "channels": [asdict(o) for o in options],
        "recommended_budget_minor": recommended,
        "organic_p50": organic,
        "target": inp.target,
    }
