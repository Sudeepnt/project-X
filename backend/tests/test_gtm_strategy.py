"""Brand book, diagnosis, feasibility forecasts, goals with pacing, and campaigns from the library."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from projectx.modules.gtm.brand.check import brand_check, claims_check, find_claims
from projectx.modules.gtm.content.fingerprint import distance, simhash, to_signed, to_unsigned
from projectx.modules.gtm.content.planner import daily_quota, mix_sequence
from projectx.modules.gtm.goals.forecast import ChannelInput, ForecastInput, forecast
from projectx.modules.gtm.models import BrandBook, ContentItem
from projectx.services import Services
from projectx.spine.decisions.models import Decision
from projectx.spine.jobs.worker import Worker


def gtm(business: dict[str, Any], path: str) -> str:
    return f"/api/v1/b/{business['id']}/gtm{path}"


async def test_brand_book_draft_needs_owner_approval(
    services: Services, founder: Any, business: dict[str, Any]
) -> None:
    res = await founder.post(gtm(business, "/brand-book/draft"), json={"notes": "Daily news for busy readers"})
    assert res.status_code == 202
    await Worker(services).run_until_idle()
    book = (await founder.get(gtm(business, "/brand-book"))).json()
    assert book["current"] is None and book["draft"] is not None
    assert book["draft"]["voice"]["tone"]
    bid = uuid.UUID(business["id"])
    async with services.db.tenant(bid) as s:
        card = await s.scalar(
            select(Decision).where(Decision.business_id == bid, Decision.action_type == "gtm.adopt_brand_book")
        )
    assert card is not None and card.status == "pending"  # adopting a voice is always the owner's call
    approved = await founder.post(f"/api/v1/b/{bid}/decisions/{card.id}/approve", json={})
    assert approved.status_code == 200
    await Worker(services).run_until_idle()
    book = (await founder.get(gtm(business, "/brand-book"))).json()
    assert book["current"]["status"] == "approved"
    audiences = (await founder.get(gtm(business, "/audiences"))).json()
    assert audiences and audiences[0]["is_primary"]
    # Approved versions are immutable; editing starts a new draft.
    edit = (await founder.post(gtm(business, "/brand-book/edit"))).json()
    assert edit["version"] == book["current"]["version"] + 1
    blocked = await founder.patch(gtm(business, f"/brand-book/{book['current']['id']}"), json={"summary": "x"})
    assert blocked.status_code == 409


def test_brand_and_claims_checks() -> None:
    book = BrandBook(
        version=1,
        status="approved",
        voice={"emoji_policy": "none", "vocabulary_avoid": ["synergy"]},
        banned_words=["guaranteed"],
        dont_list=[],
        do_list=[],
        proof_points=[],
    )
    blocked = brand_check("Guaranteed results!!! Synergy 🚀", book)
    assert not blocked.passed and blocked.severity == "block"
    assert any("never uses" in i for i in blocked.issues)
    ok = brand_check("Here is what changed today, and why it matters.", book)
    assert ok.passed
    claims = find_claims("We grew 40% last month. Readers love it. We are the best newsletter in India.")
    assert len(claims) == 2
    strict = claims_check(claims, evidence=["Subscribers grew 40% in October"], strict=True)
    assert not strict.passed and len(strict.issues) == 1  # "the best" has no evidence


def test_forecast_flags_unrealistic_goals_and_suggests_options() -> None:
    start = date(2026, 12, 1)
    small = ForecastInput(
        goal_type="demand",
        metric="new_leads",
        target=1000,
        baseline=0,
        start=start,
        deadline=start + timedelta(days=30),
        budget_minor=20_000,
        currency="USD",
        channels=[ChannelInput("instagram", 800)],
        paid_channels=["meta"],
    )
    result = forecast(small)
    assert result.verdict == "unrealistic"
    assert result.p10 <= result.p50 <= result.p90
    assert {s["kind"] for s in result.suggestions} >= {"lower_target", "switch_campaign"}
    realistic = forecast(ForecastInput(**{**small.__dict__, "target": max(result.p10, 1)}))
    assert realistic.verdict in ("likely", "stretch")
    assert forecast(small).p50 == result.p50  # deterministic for the same inputs


def test_planner_helpers() -> None:
    assert sum(daily_quota(5, i) for i in range(7)) == 5
    assert sum(daily_quota(14, i) for i in range(7)) == 14
    seq = mix_sequence({"short_video": 0.5, "carousel": 0.3, "text": 0.2}, 10)
    assert seq.count("short_video") == 5 and seq.count("carousel") == 3 and seq.count("text") == 2


def test_simhash_detects_near_duplicates() -> None:
    a = simhash("The five stories you need this morning, checked and explained in plain words for busy readers")
    b = simhash("The five stories you need this morning, checked and explained in plain words for busy people")
    c = simhash("A completely different post about pricing strategy for consulting agencies")
    assert distance(a, b) < distance(a, c)
    assert to_unsigned(to_signed(a)) == a


async def test_goal_lifecycle_with_one_active_goal(founder: Any, business: dict[str, Any]) -> None:
    diagnosis = (await founder.get(gtm(business, "/diagnosis"))).json()
    assert diagnosis["goal"]["type"] == "awareness"  # brand new business: no audience yet
    deadline = (date.today() + timedelta(days=45)).isoformat()
    preview = (
        await founder.post(
            gtm(business, "/goals/forecast"),
            json={
                "type": "demand",
                "metric": "new_subscribers",
                "target": 300,
                "deadline": deadline,
                "channels": ["instagram", "x"],
            },
        )
    ).json()
    assert preview["verdict"] in ("likely", "stretch", "unrealistic") and preview["assumptions"]
    goal = (
        await founder.post(
            gtm(business, "/goals"),
            json={
                "type": "demand",
                "metric": "new_subscribers",
                "target": 300,
                "deadline": deadline,
                "activate": True,
                "restrictions": [
                    {"kind": "allowed_platforms", "params": {"platforms": ["instagram", "x", "newsletter"]}}
                ],
            },
        )
    ).json()
    assert goal["status"] == "active" and goal["forecast"]["p50"] >= 0
    second = await founder.post(
        gtm(business, "/goals"), json={"type": "awareness", "target": 2000, "deadline": deadline, "activate": True}
    )
    assert second.status_code == 409  # only one main goal at a time
    pace = (await founder.post(gtm(business, f"/goals/{goal['id']}/pace"), json={})).json()
    assert pace["status"] in ("on_track", "behind", "at_risk", "ahead")
    detail = (await founder.get(gtm(business, f"/goals/{goal['id']}"))).json()
    assert detail["pace_history"] and detail["restrictions"][0]["kind"] == "allowed_platforms"
    unrealistic = await founder.post(
        gtm(business, "/goals"),
        json={"type": "awareness", "target": 2000, "deadline": (date.today() - timedelta(days=1)).isoformat()},
    )
    assert unrealistic.status_code == 422


async def test_campaign_recommendation_start_and_planning(
    services: Services, founder: Any, business: dict[str, Any]
) -> None:
    rec = (await founder.get(gtm(business, "/campaigns/recommendations?goal_type=demand"))).json()
    assert rec["main"]["playbook"]["key"] == "newsletter_growth"  # media business
    library = (await founder.get(gtm(business, "/campaigns/library"))).json()
    assert len(library["items"]) == 33
    paid = await founder.post(gtm(business, "/campaigns"), json={"playbook_key": "paid_lead_ads"})
    assert paid.status_code == 422  # paid campaigns need a budget
    campaign = (
        await founder.post(
            gtm(business, "/campaigns"),
            json={"playbook_key": "newsletter_growth", "start": True, "channels": ["instagram", "x", "newsletter"]},
        )
    ).json()
    assert campaign["status"] == "active"
    await Worker(services).run_until_idle()
    bid = uuid.UUID(business["id"])
    async with services.db.tenant(bid) as s:
        ideas = (
            (await s.execute(select(ContentItem).where(ContentItem.campaign_id == uuid.UUID(campaign["id"]))))
            .scalars()
            .all()
        )
    assert ideas, "starting a campaign plans a week of content"
    intents = {i.intent for i in ideas}
    assert "value" in intents
    pages = (await founder.get(gtm(business, "/landing-pages"))).json()["items"]
    assert any(p["campaign_id"] == campaign["id"] and p["kind"] == "newsletter" for p in pages)


async def test_planner_never_schedules_inside_the_review_window(
    services: Services, founder: Any, business: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Late in the evening, today's posting hours have passed: nothing may be planned in the past."""
    from datetime import UTC, datetime, timedelta

    from projectx.modules.gtm.content import planner
    from projectx.modules.gtm.models import Campaign
    from projectx.spine.tenancy.models import Business

    campaign = (
        await founder.post(
            gtm(business, "/campaigns"), json={"playbook_key": "newsletter_growth", "channels": ["instagram", "x"]}
        )
    ).json()
    bid = uuid.UUID(business["id"])
    late = datetime.now(UTC).replace(hour=22, minute=30, second=0, microsecond=0)
    monkeypatch.setattr(planner, "utcnow", lambda: late)
    async with services.db.tenant(bid) as s:
        biz = await s.get(Business, bid)
        camp = await s.get(Campaign, uuid.UUID(campaign["id"]))
        assert biz is not None and camp is not None
        slots = await planner.plan_slots(s, biz, camp, start_day=late.date(), days=3)
    assert slots, "later days still get content"
    assert all(slot.when >= late + timedelta(hours=1) for slot in slots)


async def test_budget_planner_shows_diminishing_returns(founder: Any, business: dict[str, Any]) -> None:
    from datetime import date, timedelta

    from projectx.modules.gtm.goals.forecast import saturation

    assert saturation(0, "meta") == 1.0 and saturation(400, "meta") == 0.5
    await founder.post(gtm(business, "/channels/practice"), json={"platform": "instagram", "followers": 5000})
    deadline = (date.today() + timedelta(days=60)).isoformat()
    res = await founder.post(
        gtm(business, "/goals/budget-plan"),
        json={
            "type": "demand",
            "target": 2000,
            "deadline": deadline,
            "budget_minor": 3_000_000,
            "paid_channels": ["meta", "google"],
        },
    )
    assert res.status_code == 200, res.text
    plan = res.json()
    p50s = [lv["p50"] for lv in plan["levels"]]
    assert p50s == sorted(p50s), "more budget never forecasts fewer results"
    costs = [lv["cost_per_extra_minor"] for lv in plan["levels"] if lv["cost_per_extra_minor"]]
    assert costs[-1] > costs[0], "each extra result costs more as spend grows"
    assert {c["channel"] for c in plan["channels"]} == {"meta", "google"}
    assert 0 < plan["recommended_budget_minor"] <= 9_000_000
    zero = await founder.post(
        gtm(business, "/goals/budget-plan"), json={"type": "demand", "target": 10, "deadline": deadline}
    )
    assert zero.status_code == 422
