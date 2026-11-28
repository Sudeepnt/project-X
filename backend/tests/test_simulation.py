"""The simulation: calibrated from every department, deterministic, and its levers move the numbers the right way."""

from __future__ import annotations

import uuid
from typing import Any

from projectx.modules.strategy import simulation as sim
from projectx.services import Services
from projectx.spine.tenancy.models import Business


def base(**over: Any) -> sim.Baseline:
    values: dict[str, Any] = {
        "currency": "INR",
        "cash_minor": 2_000_000_00,
        "receivable_minor": 0,
        "deals_per_month": 3.0,
        "deal_log_mean": 11.5,  # about 1 lakh
        "deal_log_sd": 0.5,
        "pipeline": [],
        "mrr_minor": 0,
        "arpu_minor": 0,
        "new_subs_per_month": 0.0,
        "churn_monthly": 0.05,
        "collection_rate": 0.95,
        "collection_delay_months": 1,
        "payroll_monthly_minor": 300_000_00,
        "software_monthly_minor": 10_000_00,
        "other_costs_monthly_minor": 50_000_00,
        "other_costs_sd_minor": 5_000_00,
    }
    return sim.Baseline(**(values | over))


def test_same_question_same_answer_and_levers_move_the_right_way() -> None:
    b = base()
    q = sim.Question(months=12, goals=[sim.Goal(kind="cash_floor", target_minor=0, by_month=12)])
    assert sim.simulate(b, q) == sim.simulate(b, q)  # a fixed seed: repeatable

    plain = sim.simulate(b, q)
    hire = sim.simulate(
        b, q.model_copy(update={"scenario": sim.Scenario(hires=[sim.Hire(monthly_cost_minor=100_000_00)])})
    )
    assert hire["months"][-1]["cash"]["p50"] < plain["months"][-1]["cash"]["p50"]
    marketing = sim.simulate(b, q.model_copy(update={"scenario": sim.Scenario(marketing_monthly_minor=400_000_00)}))
    assert marketing["revenue_total"]["p50"] > plain["revenue_total"]["p50"]
    raise_cash = sim.simulate(b, q.model_copy(update={"scenario": sim.Scenario(cash_injection_minor=5_000_000_00)}))
    assert raise_cash["chance_cash_runs_out"] <= plain["chance_cash_runs_out"]


def test_goal_odds_fall_as_targets_rise_and_pipeline_counts() -> None:
    b = base(pipeline=[(10_00_000_00, 0.9, 1)])  # a big deal, very likely next month
    odds = []
    for target in (10_00_000_00, 40_00_000_00, 200_00_000_00):
        q = sim.Question(months=6, goals=[sim.Goal(kind="revenue_total", target_minor=target, by_month=6)])
        odds.append(sim.simulate(b, q)["goals"][0]["probability"])
    assert odds[0] >= odds[1] >= odds[2]
    assert odds[0] > 0.85 and odds[2] < 0.05
    without = sim.simulate(
        base(), sim.Question(months=6, goals=[sim.Goal(kind="revenue_total", target_minor=10_00_000_00, by_month=6)])
    )
    assert without["goals"][0]["probability"] < odds[0]


def test_subscriptions_churn_and_price() -> None:
    b = base(deals_per_month=0.0, mrr_minor=100 * 1_000_00, arpu_minor=1_000_00, churn_monthly=0.1)
    q = sim.Question(months=6, goals=[sim.Goal(kind="mrr", target_minor=60 * 1_000_00, by_month=6)])
    churny = sim.simulate(b, q)
    assert churny["months"][-1]["mrr"]["p50"] < churny["months"][0]["mrr"]["p50"]
    better = sim.simulate(b, q.model_copy(update={"scenario": sim.Scenario(churn_change_pct=-50)}))
    assert better["goals"][0]["probability"] >= churny["goals"][0]["probability"]
    dearer = sim.simulate(b, q.model_copy(update={"scenario": sim.Scenario(price_change_pct=20)}))
    assert dearer["months"][0]["mrr"]["p50"] > churny["months"][0]["mrr"]["p50"]


async def test_calibrates_from_every_department(services: Services, founder: Any, business: dict[str, Any]) -> None:
    url = f"/api/v1/b/{business['id']}"
    pipelines = (await founder.get(f"{url}/gtm/pipelines")).json()["items"]
    advertisers = next(p for p in pipelines if p["key"] == "advertisers")
    for value in (80_000, 120_000):
        d = (
            await founder.post(
                f"{url}/gtm/deals", json={"pipeline_id": advertisers["id"], "title": "Ad", "value_minor": value}
            )
        ).json()
        await founder.patch(f"{url}/gtm/deals/{d['id']}", json={"stage_key": "advertiser"})
    await founder.post(
        f"{url}/gtm/deals", json={"pipeline_id": advertisers["id"], "title": "Maybe", "value_minor": 50_000}
    )
    await founder.post(
        f"{url}/people/employees",
        json={
            "name": "Ira",
            "email": "ira@example.com",
            "role": "Editor",
            "start_date": "2026-01-05",
            "monthly_salary_minor": 250_000,
        },
    )
    await founder.post(
        f"{url}/admin/assets",
        json={"kind": "software", "name": "Design tool", "cost_minor": 12_000, "billing": "yearly"},
    )
    found = {a["name"]: a for a in (await founder.get(f"{url}/strategy/simulate/baseline")).json()["assumptions"]}
    assert found["New deals a month"]["source"].startswith("your data: 2 won")
    assert found["Open deals in the pipeline"]["value"] == 1
    assert found["Payroll a month"]["value"] == 250_000
    assert found["Software a month"]["value"] == 1_000

    async with services.db.tenant(uuid.UUID(business["id"])) as s:
        biz = await s.get(Business, uuid.UUID(business["id"]))
        assert biz is not None
        b = await sim.calibrate(s, biz)
    assert b.deals_per_month == 2 / 3 and b.payroll_monthly_minor == 250_000


async def test_ask_in_plain_words(founder: Any, business: dict[str, Any]) -> None:
    url = f"/api/v1/b/{business['id']}/strategy"
    res = await founder.post(
        f"{url}/simulate/ask",
        json={"text": "What if I hire 2 salespeople at 50k a month? Will we hit 10 lakh revenue in 6 months?"},
    )
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["status"] == "done"
    q = out["question"]
    assert q["months"] == 6 and len(q["scenario"]["hires"]) == 2
    assert (
        q["scenario"]["hires"][0]["monthly_cost_minor"] == 50_000 * 100
        and q["scenario"]["hires"][0]["adds_deals_per_month"] == 1.0
    )
    goal = q["goals"][0]
    assert (goal["kind"], goal["target_minor"], goal["by_month"]) == ("revenue_total", 10 * 100_000 * 100, 6)
    assert "% likely" in out["summary"] and len(out["scenario"]["months"]) == 6
    assert {"baseline", "scenario", "difference", "assumptions"} <= set(out)

    stored = (await founder.get(f"{url}/simulations")).json()["items"]
    assert stored[0]["id"] == out["id"] and stored[0]["asked"].startswith("What if I hire")
    again = (await founder.get(f"{url}/simulations/{out['id']}")).json()
    assert again["scenario"] == out["scenario"]

    vague = (await founder.post(f"{url}/simulate/ask", json={"text": "tell me something"})).json()
    assert vague["status"] == "asked"

    exact = await founder.post(
        f"{url}/simulate",
        json={
            "months": 3,
            "scenario": {"cost_cut_monthly_minor": 1_000_000},
            "goals": [{"kind": "cash_floor", "target_minor": 0, "by_month": 3}],
        },
    )
    assert exact.status_code == 200 and exact.json()["scenario"]["goals"][0]["probability"] >= 0
    bad = await founder.post(f"{url}/simulate", json={"months": 99})
    assert bad.status_code == 422
