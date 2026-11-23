"""Picking campaigns: one main campaign plus up to two supporting ones, from the library.

Ranking is transparent: every playbook gets a score and the reasons behind it, so the owner sees
why a campaign was recommended and why another was not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from projectx.modules.gtm.campaigns.library import CATEGORY_FOR_GOAL, TIERS, Playbook, playbooks


@dataclass
class SelectionContext:
    goal_type: str
    business_model: str
    plan: str
    followers: int = 0
    budget_minor: int = 0
    b2b: bool = False
    customers: int = 0
    leads: int = 0
    launching: bool = False
    local: bool = False
    learned: dict[str, float] = field(default_factory=dict)  # playbook key -> past performance score 0..1


@dataclass
class Ranked:
    playbook: Playbook
    score: float
    reasons: list[str]
    blocked: str | None = None

    def view(self) -> dict[str, Any]:
        return {
            "playbook": self.playbook.view(),
            "score": round(self.score, 2),
            "reasons": self.reasons,
            "blocked": self.blocked,
        }


def _score(p: Playbook, ctx: SelectionContext) -> Ranked:
    score, reasons = 1.0, []
    w = p.when
    if p.goals and p.goals[0] == ctx.goal_type:
        score += 2
        reasons.append("Built for this goal")
    if p.category == CATEGORY_FOR_GOAL.get(ctx.goal_type):
        score += 1
    if w.get("needs_budget") and ctx.budget_minor <= 0:
        return Ranked(p, 0, reasons, "Needs an ad budget")
    if "min_followers" in w and ctx.followers < int(w["min_followers"]):
        score -= 1.5
        reasons.append("Works better once you have more followers")
    if "max_followers" in w and ctx.followers > int(w["max_followers"]):
        score -= 1
    if w.get("business_models"):
        if ctx.business_model in w["business_models"]:
            score += 1.5
            reasons.append(f"Fits {ctx.business_model} businesses")
        else:
            score -= 1
    if w.get("b2b") and not ctx.b2b:
        score -= 1.5
    elif w.get("b2b") and ctx.b2b:
        score += 1
        reasons.append("Fits selling to businesses")
    if "min_customers" in w and ctx.customers < int(w["min_customers"]):
        return Ranked(p, 0, reasons, "Needs existing customers")
    if "min_leads" in w and ctx.leads < int(w["min_leads"]):
        score -= 1.5
        reasons.append("Works best with a pool of leads")
    if w.get("launching") and not ctx.launching:
        score -= 2.5
    elif w.get("launching"):
        score += 3
        reasons.append("You're launching something")
    if w.get("local") and not ctx.local:
        score -= 2
    if p.runs == "ai":
        score += 0.5
        reasons.append("Runs on its own")
    if ctx.learned.get(p.key) is not None:
        score += 2 * (ctx.learned[p.key] - 0.5)
        reasons.append("Has worked here before" if ctx.learned[p.key] >= 0.5 else "Underperformed here before")
    blocked = None
    if TIERS.index(p.tier) > TIERS.index(ctx.plan if ctx.plan in TIERS else "free"):
        blocked = f"Needs the {p.tier.capitalize()} plan"
        score -= 0.5
    return Ranked(p, score, reasons, blocked)


def rank(ctx: SelectionContext) -> list[Ranked]:
    ranked = [_score(p, ctx) for p in playbooks(goal=ctx.goal_type)]
    return sorted(ranked, key=lambda r: (r.blocked is None and r.score > 0, r.score), reverse=True)


def recommend(ctx: SelectionContext) -> dict[str, Any]:
    ranked = rank(ctx)
    usable = [r for r in ranked if r.blocked is None and r.score > 0]
    main_category = CATEGORY_FOR_GOAL.get(ctx.goal_type)
    main = next((r for r in usable if r.playbook.category == main_category), usable[0] if usable else None)
    supporting: list[Ranked] = []
    for r in usable:
        if main is not None and r.playbook.key == main.playbook.key:
            continue
        if len(supporting) == 2:
            break
        # Prefer supporting campaigns that add a different channel mix.
        if main is not None and set(r.playbook.channels) <= set(main.playbook.channels) and r.playbook.channels:
            continue
        supporting.append(r)
    return {
        "main": main.view() if main else None,
        "supporting": [r.view() for r in supporting],
        "others": [r.view() for r in ranked if r not in supporting and r is not main][:12],
    }
