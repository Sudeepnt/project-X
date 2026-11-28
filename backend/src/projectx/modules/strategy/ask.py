"""Ask the simulation in plain words: "what if I hire two salespeople?", "will we hit 10 lakh revenue by March?".

An open model (or, with no model, a small built-in parser) turns the words into settings; the settings are checked
against their allowed ranges; the simulation runs; the answer is written by code from the numbers. If the question
can't be turned into settings, it says what it understood and asks.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from projectx.kernel.errors import ValidationFailed
from projectx.kernel.time import today_in
from projectx.modules.strategy import simulation as sim
from projectx.modules.strategy.models import SimulationRun
from projectx.services import Services
from projectx.spine.gateway.contracts import ChatMessage, GatewayError, ModelRequest
from projectx.spine.gateway.offline import offline_generator
from projectx.spine.tenancy.models import Business


class Draft(BaseModel):
    understood: str = Field(description="One sentence: what the person wants to know")
    clarify: str = Field(description="A short question if the numbers can't be worked out; else empty")
    question: sim.Question


_SYSTEM = """You turn a founder's what-if question into settings for a business simulation. Today is {today}. The
business's currency is {currency}; amounts are in minor units (1 {currency} = 100). "Lakh" is 100,000 and "crore" is
10,000,000 in major units. "by March" means the number of months from now until the end of that month.
Only set what the question changes; leave everything else at its default. Goals: revenue_total (total revenue over
the months), monthly_revenue (in one month), mrr (recurring revenue in one month), cash_floor (never below an amount).
If the question asks "will we survive" or nothing about a goal, add a cash_floor goal of 0 over the whole period."""


async def ask(services: Services, business_id: uuid.UUID, user_id: uuid.UUID | None, text: str) -> dict[str, Any]:
    async with services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        assert business is not None
        today, currency = today_in(business.timezone), business.currency
    try:
        draft = (
            await services.gateway.run(
                business_id,
                ModelRequest(
                    task="strategy.simulation.ask",
                    system=_SYSTEM.format(today=today.isoformat(), currency=currency),
                    messages=[ChatMessage("user", text[:2000])],
                    output_schema=Draft,
                    max_tokens=1200,
                    rung="small",
                    max_rung="mid",
                    context={"text": text, "today": today.isoformat()},
                ),
            )
        ).parsed_as(Draft)
    except (GatewayError, ValidationError) as exc:
        raise ValidationFailed(f"Couldn't read that question: {exc}") from exc
    if draft.clarify.strip() and not (draft.question.goals or draft.question.scenario != sim.Scenario()):
        return {"status": "asked", "understood": draft.understood, "reply": draft.clarify}
    return await run(services, business_id, user_id, draft.question, asked=text, understood=draft.understood)


async def run(
    services: Services,
    business_id: uuid.UUID,
    user_id: uuid.UUID | None,
    q: sim.Question,
    *,
    asked: str = "",
    understood: str = "",
) -> dict[str, Any]:
    if not q.goals:
        q = q.model_copy(
            update={
                "goals": [sim.Goal(kind="cash_floor", target_minor=0, by_month=q.months, label="Never run out of cash")]
            }
        )
    async with services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        assert business is not None
        baseline = await sim.calibrate(session, business)
    result = sim.compare(baseline, q)
    summary = sim.explain(result)
    async with services.db.tenant(business_id) as session:
        row = SimulationRun(
            business_id=business_id,
            asked=asked[:2000],
            question=q.model_dump(),
            result=result,
            summary=summary[:2000],
            created_by=user_id,
        )
        session.add(row)
        await session.flush()
        run_id = row.id
    return {"status": "done", "id": str(run_id), "understood": understood, "summary": summary, **result}


# ---------------------------------------------------------------- offline: the plain cases, no model needed

_UNITS = {
    "k": 1_000,
    "thousand": 1_000,
    "l": 100_000,
    "lakh": 100_000,
    "lakhs": 100_000,
    "lac": 100_000,
    "cr": 10_000_000,
    "crore": 10_000_000,
    "m": 1_000_000,
    "million": 1_000_000,
}
_AMOUNT = r"(?:rs\.?|inr|\$|₹|usd)?\s*([\d][\d,]*(?:\.\d+)?)\s*(k|thousand|lakhs?|lac|l|cr|crore|m|million)?\b"
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def _minor(number: str, unit: str | None) -> int:
    return round(float(number.replace(",", "")) * _UNITS.get((unit or "").lower(), 1) * 100)


def _months_until(word: str, today: date) -> int | None:
    m = re.match(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", word.lower())
    if not m:
        return None
    month = _MONTHS.index(m.group(1)) + 1
    ahead = (month - today.month) % 12
    return max(ahead, 1) if ahead else 12


@offline_generator("strategy.simulation.ask")
def _offline(req: ModelRequest) -> dict[str, Any]:
    text = str(req.context.get("text", "")).lower()
    today = date.fromisoformat(str(req.context.get("today")))
    scenario: dict[str, Any] = {"hires": []}
    goals: list[dict[str, Any]] = []
    months = 12
    if m := re.search(r"(\d+)\s*months?", text):
        months = max(1, min(int(m.group(1)), 36))
    if m := re.search(r"by (?:the end of )?(\w+)", text):
        months = _months_until(m.group(1), today) or months
    if m := re.search(r"(raise|increase|cut|lower|drop)\w* (?:our |the )?prices? (?:by )?(\d+(?:\.\d+)?)\s*%", text):
        pct = float(m.group(2))
        scenario["price_change_pct"] = pct if m.group(1) in ("raise", "increase") else -pct
    if m := re.search(r"hire (\d+|a|an|one|two|three|four|five)\s+(\w+)", text):
        count = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}.get(m.group(1)) or int(
            m.group(1)
        )
        cost = re.search(r"at " + _AMOUNT, text)
        monthly = _minor(cost.group(1), cost.group(2)) if cost else 5_000_000
        sales = "sales" in m.group(2)
        scenario["hires"] = [
            {"monthly_cost_minor": monthly, "start_month": 1, "adds_deals_per_month": 1.0 if sales else 0.0}
        ] * min(count, 50)
    if m := re.search(r"(?:spend|put) " + _AMOUNT + r"[^.]*?(?:marketing|ads|advertising)", text):
        scenario["marketing_monthly_minor"] = _minor(m.group(1), m.group(2))
    if m := re.search(r"cut costs? (?:by )?" + _AMOUNT, text):
        scenario["cost_cut_monthly_minor"] = _minor(m.group(1), m.group(2))
    m = re.search(r"(?:raise|raised|loan|invest\w*) (?:of )?" + _AMOUNT, text)
    if m and "price" not in text[max(0, m.start() - 5) : m.end() + 10]:
        scenario["cash_injection_minor"] = _minor(m.group(1), m.group(2))
    if m := re.search(r"(?:hit|reach|make|get to)\s+" + _AMOUNT + r"\s*(?:in )?(?:revenue|sales)?", text):
        near = text[m.start() : m.end() + 25]  # only the goal's own words decide its kind, not the rest of the question
        kind = (
            "mrr"
            if "mrr" in near or "recurring" in near
            else "monthly_revenue"
            if any(w in near for w in ("a month", "per month", "monthly", "/month"))
            else "revenue_total"
        )
        goals.append(
            {"kind": kind, "target_minor": _minor(m.group(1), m.group(2)), "by_month": months, "label": "Revenue goal"}
        )
    changed = any(v for k, v in scenario.items() if k != "hires") or scenario["hires"]
    return {
        "understood": text[:200],
        "clarify": ""
        if (changed or goals or "survive" in text or "runway" in text)
        else "What would you change, or which target should I check?",
        "question": {
            "months": months,
            "scenario": {
                "price_change_pct": scenario.get("price_change_pct", 0.0),
                "deal_flow_change_pct": 0.0,
                "churn_change_pct": 0.0,
                "hires": scenario["hires"],
                "marketing_monthly_minor": scenario.get("marketing_monthly_minor", 0),
                "cost_cut_monthly_minor": scenario.get("cost_cut_monthly_minor", 0),
                "cash_injection_minor": scenario.get("cash_injection_minor", 0),
            },
            "goals": goals,
            "marketing_deals_per_100k": 0.5,
        },
    }
