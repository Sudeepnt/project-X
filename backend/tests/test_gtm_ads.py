"""Ads engine: approvals, caps in two places, restricted categories, stop rules, platform adapters."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

import httpx
import orjson
import pytest
import respx
from sqlalchemy import select, text

from projectx.kernel.time import today_in
from projectx.modules.gtm.ads.adapters import AdsContext, AdSpec, GoogleAds, MetaAds
from projectx.modules.gtm.ads.service import days_left_in_month
from projectx.modules.gtm.models import AdCampaign, AdMetricDaily
from projectx.services import Services
from projectx.spine.decisions.models import Decision
from projectx.spine.jobs.worker import Worker


def ads(business: dict[str, Any], path: str = "") -> str:
    return f"/api/v1/b/{business['id']}/gtm/ads{path}"


async def _run(services: Services) -> None:
    for _ in range(4):
        async with services.db.session() as s:
            await s.execute(text("UPDATE jobs SET run_at = now() WHERE status = 'queued' AND run_at > now()"))
        if await Worker(services).run_until_idle() == 0:
            return


async def _account(founder: Any, business: dict[str, Any], cap: int = 1_000_000) -> dict[str, Any]:
    res = await founder.post(ads(business, "/accounts/practice"), json={"platform": "meta", "monthly_cap_minor": cap})
    assert res.status_code == 201, res.text
    body: dict[str, Any] = res.json()
    return body


async def _campaign(founder: Any, business: dict[str, Any], account: dict[str, Any], **extra: Any) -> Any:
    return await founder.post(
        ads(business, "/campaigns"),
        json={
            "ad_account_id": account["id"],
            "name": "Explainers that worked",
            "daily_budget_minor": 3000,
            "landing_url": "https://example.com/newsletter",
            **extra,
        },
    )


async def _seed_metrics(
    services: Services, business: dict[str, Any], ad_id: str, days: list[tuple[int, int, int]]
) -> None:
    """(days_ago, spend_minor, leads)"""
    bid = uuid.UUID(business["id"])
    async with services.db.tenant(bid) as s:
        today = today_in("Asia/Kolkata")
        for ago, spend, leads in days:
            s.add(
                AdMetricDaily(
                    business_id=bid,
                    ad_campaign_id=uuid.UUID(ad_id),
                    day=today - timedelta(days=ago),
                    spend_minor=spend,
                    impressions=spend,
                    clicks=spend // 100,
                    leads=leads,
                )
            )
        ad = await s.get(AdCampaign, uuid.UUID(ad_id))
        assert ad is not None
        ad.starts_on = today - timedelta(days=10)


async def test_starting_ads_always_asks_then_runs(services: Services, founder: Any, business: dict[str, Any]) -> None:
    account = await _account(founder, business)
    res = await _campaign(founder, business, account)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["status"] == "waiting_for_approval"  # never automatic, whatever the autonomy setting
    decision = (await founder.get(f"/api/v1/b/{business['id']}/decisions/{body['decision_id']}")).json()
    assert decision["forecast"]["monthly_cap_minor"] == 1_000_000
    assert decision["cost_minor"] == 3000
    ok = await founder.post(f"/api/v1/b/{business['id']}/decisions/{body['decision_id']}/approve", json={})
    assert ok.status_code == 200, ok.text
    await _run(services)
    campaign = next(
        c for c in (await founder.get(ads(business))).json()["campaigns"] if c["id"] == body["campaign"]["id"]
    )
    assert campaign["status"] == "active"
    assert campaign["external_id"].startswith("sim-")


async def test_caps_block_spend_over_the_monthly_ceiling(founder: Any, business: dict[str, Any]) -> None:
    today = today_in("Asia/Kolkata")
    tiny = 3000 * days_left_in_month(today) - 1  # one unit short of the rest of the month at 30/day
    account = await _account(founder, business, cap=tiny)
    body = (await _campaign(founder, business, account)).json()
    assert body["status"] == "blocked"
    assert any("cap" in r for r in body["reasons"])
    assert body["campaign"]["status"] == "blocked"
    no_cap = await _account(founder, business, cap=0)
    assert (await _campaign(founder, business, no_cap)).json()["status"] == "blocked"


async def test_restricted_categories(founder: Any, business: dict[str, Any]) -> None:
    account = await _account(founder, business)
    politics = await _campaign(founder, business, account, special_category="politics")
    assert politics.status_code == 403
    housing = await _campaign(founder, business, account, special_category="housing", audience_kind="lookalike")
    assert housing.status_code == 422
    fine = await _campaign(founder, business, account, special_category="housing", audience_kind="retargeting")
    assert fine.status_code == 201


async def test_budget_changes_and_stop_rules(services: Services, founder: Any, business: dict[str, Any]) -> None:
    account = await _account(founder, business)
    body = (await _campaign(founder, business, account, target_cpl_minor=500)).json()
    await founder.post(f"/api/v1/b/{business['id']}/decisions/{body['decision_id']}/approve", json={})
    await _run(services)
    ad_id = body["campaign"]["id"]

    lower = (await founder.post(ads(business, f"/campaigns/{ad_id}/budget"), json={"daily_budget_minor": 2000})).json()
    assert lower["status"] == "applied"  # less spend needs nobody's permission
    higher = (await founder.post(ads(business, f"/campaigns/{ad_id}/budget"), json={"daily_budget_minor": 4000})).json()
    assert higher["status"] == "waiting_for_approval"  # more spend always asks

    # Three days of budget with zero results: the engine pauses it (pausing runs on its own).
    await _seed_metrics(services, business, ad_id, [(3, 2000, 0), (4, 2000, 0), (5, 2000, 0)])
    async with services.db.tenant(uuid.UUID(business["id"])) as s:
        ad = await s.get(AdCampaign, uuid.UUID(ad_id))
        assert ad is not None
        ad.rules = {**(ad.rules or {}), "simulation": {"sim_quality": 0}}
    await founder.post(ads(business, "/sync"))
    await _run(services)
    campaign = next(c for c in (await founder.get(ads(business))).json()["campaigns"] if c["id"] == ad_id)
    assert campaign["status"] == "paused"
    assert "no results" in campaign["paused_reason"]
    async with services.db.tenant(uuid.UUID(business["id"])) as s:
        pauses = (await s.execute(select(Decision).where(Decision.action_type == "gtm.pause_ad"))).scalars().all()
    assert len(pauses) == 1, "one pause, not one per check"

    # Resuming is the founder's call, still inside the caps.
    resumed = await founder.post(ads(business, f"/campaigns/{ad_id}/resume"))
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["campaign"]["status"] == "active"


async def test_lowering_the_cap_below_spend_pauses_everything(
    services: Services, founder: Any, business: dict[str, Any]
) -> None:
    account = await _account(founder, business)
    body = (await _campaign(founder, business, account)).json()
    await founder.post(f"/api/v1/b/{business['id']}/decisions/{body['decision_id']}/approve", json={})
    await _run(services)
    ad_id = body["campaign"]["id"]
    await founder.post(ads(business, "/sync"))
    await _run(services)  # today's spend arrives from the platform
    capped = await founder.put(ads(business, f"/accounts/{account['id']}/cap"), json={"monthly_cap_minor": 100})
    assert capped.status_code == 200, capped.text
    await _run(services)
    campaign = next(c for c in (await founder.get(ads(business))).json()["campaigns"] if c["id"] == ad_id)
    assert campaign["status"] == "paused"
    assert campaign["paused_reason"] == "Monthly ad cap reached"
    blocked = await founder.post(ads(business, f"/campaigns/{ad_id}/resume"))
    assert blocked.status_code == 403  # can't restart past the cap


def _ctx(http: httpx.AsyncClient, platform: str) -> AdsContext:
    if platform == "meta":
        return AdsContext(
            http=http,
            credentials={"access_token": "tok"},
            account_external_id="123",
            settings={"page_id": "999"},
            currency="USD",
        )
    return AdsContext(
        http=http,
        credentials={"access_token": "tok", "developer_token": "dev"},
        account_external_id="123-456-7890",
        settings={},
        currency="USD",
    )


@respx.mock
async def test_meta_adapter_builds_the_chain_and_reads_insights() -> None:
    base = "https://graph.facebook.com/v24.0"
    camp = respx.post(f"{base}/act_123/campaigns").mock(return_value=httpx.Response(200, json={"id": "c1"}))
    adset = respx.post(f"{base}/act_123/adsets").mock(return_value=httpx.Response(200, json={"id": "s1"}))
    respx.post(f"{base}/act_123/adcreatives").mock(return_value=httpx.Response(200, json={"id": "cr1"}))
    respx.post(f"{base}/act_123/ads").mock(return_value=httpx.Response(200, json={"id": "a1"}))
    activate = respx.post(f"{base}/c1").mock(return_value=httpx.Response(200, json={"success": True}))
    respx.get(f"{base}/c1/insights").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "date_start": "2026-11-20",
                        "spend": "12.34",
                        "impressions": "1000",
                        "clicks": "20",
                        "actions": [{"action_type": "lead", "value": "3"}],
                    },
                ]
            },
        )
    )
    async with httpx.AsyncClient() as http:
        meta = MetaAds()
        created = await meta.create(
            _ctx(http, "meta"),
            AdSpec(
                name="Test",
                objective="leads",
                daily_budget_minor=2500,
                audience_kind="cold",
                special_category="credit",
                country="IN",
                landing_url="https://example.com",
            ),
        )
        assert created.external_id == "c1"
        sent = dict(httpx.QueryParams(camp.calls[0].request.content.decode()))
        assert sent["status"] == "PAUSED"  # activated only after the whole chain exists
        assert orjson.loads(sent["special_ad_categories"]) == ["FINANCIAL_PRODUCTS_SERVICES"]
        assert dict(httpx.QueryParams(adset.calls[0].request.content.decode()))["daily_budget"] == "2500"
        assert activate.called
        rows = await meta.daily_metrics(_ctx(http, "meta"), "c1", date(2026, 11, 20), date(2026, 11, 20))
    assert rows[0].spend_minor == 1234 and rows[0].leads == 3


@respx.mock
async def test_meta_auth_errors_surface_as_reconnect() -> None:
    respx.post("https://graph.facebook.com/v24.0/c1").mock(
        return_value=httpx.Response(400, json={"error": {"message": "Session expired", "code": 190}})
    )
    from projectx.modules.gtm.channels.adapters.base import AuthExpired

    async with httpx.AsyncClient() as http:
        with pytest.raises(AuthExpired):
            await MetaAds().set_status(_ctx(http, "meta"), "c1", False)


@respx.mock
async def test_google_adapter_links_budgets_and_reads_metrics() -> None:
    base = "https://googleads.googleapis.com/v21/customers/1234567890"

    def search(request: httpx.Request) -> httpx.Response:
        query = orjson.loads(request.content)["query"]
        if "segments.date" in query:
            return httpx.Response(
                200,
                json=[
                    {
                        "results": [
                            {
                                "segments": {"date": "2026-11-20"},
                                "metrics": {
                                    "costMicros": "5000000",
                                    "impressions": "100",
                                    "clicks": "7",
                                    "conversions": "2.0",
                                },
                            }
                        ]
                    }
                ],
            )
        return httpx.Response(
            200,
            json=[
                {"results": [{"campaign": {"id": "42", "campaignBudget": "customers/1234567890/campaignBudgets/9"}}]}
            ],
        )

    respx.post(f"{base}/googleAds:searchStream").mock(side_effect=search)
    budget = respx.post(f"{base}/campaignBudgets:mutate").mock(return_value=httpx.Response(200, json={}))
    status = respx.post(f"{base}/campaigns:mutate").mock(return_value=httpx.Response(200, json={}))
    async with httpx.AsyncClient() as http:
        g = GoogleAds()
        created = await g.create(
            _ctx(http, "google"),
            AdSpec(
                name="x",
                objective="leads",
                daily_budget_minor=1500,
                audience_kind="cold",
                special_category=None,
                country=None,
                landing_url=None,
                external_campaign_id="42",
            ),
        )
        assert created.external_id == "42"
        assert orjson.loads(budget.calls[0].request.content)["operations"][0]["update"]["amountMicros"] == "15000000"
        assert orjson.loads(status.calls[0].request.content)["operations"][0]["update"]["status"] == "ENABLED"
        assert budget.calls[0].request.headers["developer-token"] == "dev"
        rows = await g.daily_metrics(_ctx(http, "google"), "42", date(2026, 11, 20), date(2026, 11, 20))
    assert rows[0].spend_minor == 500 and rows[0].leads == 2
