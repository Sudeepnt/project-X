"""Billing with Dodo Payments: checkout, plan changes from signed webhooks only, replays, nudges, ad share."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from datetime import timedelta
from typing import Any

import httpx
import orjson
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import select

from projectx.kernel.time import today_in
from projectx.modules.gtm.models import AdAccount, AdCampaign, AdMetricDaily
from projectx.services import Services
from projectx.spine.billing.models import Subscription
from projectx.spine.billing.plans import add_usage
from projectx.spine.jobs.queue import enqueue
from projectx.spine.jobs.worker import Worker
from projectx.spine.notifications.models import Notification
from projectx.spine.tenancy.models import Business
from tests.conftest import new_business

SECRET = "whsec_" + base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
DODO = "https://test.dodopayments.com"
PRODUCTS = {"autopilot": "pdt_auto", "growth": "pdt_growth", "scale": "pdt_scale"}


@pytest.fixture
def dodo(services: Services, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services.settings, "dodo_api_key", SecretStr("sk_test"))
    monkeypatch.setattr(services.settings, "dodo_webhook_secret", SecretStr(SECRET))
    monkeypatch.setattr(services.settings, "dodo_product_ids", PRODUCTS)


def _signed(body: dict[str, Any], *, msg_id: str | None = None, secret: str = SECRET) -> tuple[bytes, dict[str, str]]:
    raw = orjson.dumps(body)
    msg_id = msg_id or f"msg_{uuid.uuid4().hex}"
    ts = str(int(time.time()))
    key = base64.b64decode(secret.removeprefix("whsec_"))
    sig = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + raw, hashlib.sha256).digest()).decode()
    return raw, {
        "webhook-id": msg_id,
        "webhook-timestamp": ts,
        "webhook-signature": f"v1,{sig}",
        "content-type": "application/json",
    }


def _event(kind: str, business_id: str, product: str = "pdt_growth", **data: Any) -> dict[str, Any]:
    return {
        "type": kind,
        "data": {
            "subscription_id": "sub_1",
            "customer": {"customer_id": "cus_1"},
            "product_id": product,
            "metadata": {"business_id": business_id},
            "next_billing_date": "2026-12-23T00:00:00Z",
            **data,
        },
    }


async def _plan(services: Services, business_id: str) -> tuple[str, str]:
    async with services.db.tenant(uuid.UUID(business_id)) as s:
        b = await s.get(Business, uuid.UUID(business_id))
        sub = await s.get(Subscription, uuid.UUID(business_id))
        assert b is not None and sub is not None
        return b.plan, sub.status


async def test_webhook_rejects_bad_signatures(client: httpx.AsyncClient, dodo: None, business: dict[str, Any]) -> None:
    raw, headers = _signed(
        _event("subscription.active", business["id"]), secret="whsec_" + base64.b64encode(b"x" * 32).decode()
    )
    res = await client.post("/webhooks/billing/dodo", content=raw, headers=headers)
    assert res.status_code == 401


@respx.mock
async def test_checkout_then_webhook_changes_plan_once(
    services: Services, client: httpx.AsyncClient, founder: Any, dodo: None
) -> None:
    business = await new_business(founder)
    bid = business["id"]
    async with services.db.tenant(uuid.UUID(bid)) as s:  # start from Free, like a real new customer
        b = await s.get(Business, uuid.UUID(bid))
        assert b is not None
        b.plan = "free"
    route = respx.post(f"{DODO}/checkouts").mock(
        return_value=httpx.Response(
            200, json={"session_id": "cks_1", "checkout_url": "https://checkout.dodopayments.com/cks_1"}
        )
    )
    res = await founder.post(f"/api/v1/b/{bid}/billing/checkout", json={"plan": "growth"})
    assert res.status_code == 200, res.text
    assert res.json() == {"mode": "checkout", "url": "https://checkout.dodopayments.com/cks_1"}
    sent = orjson.loads(route.calls[0].request.content)
    assert sent["product_cart"] == [{"product_id": "pdt_growth", "quantity": 1}]
    assert sent["metadata"]["business_id"] == bid
    assert route.calls[0].request.headers["authorization"] == "Bearer sk_test"
    assert (await _plan(services, bid))[0] == "free", "coming back from checkout changes nothing by itself"

    raw, headers = _signed(_event("subscription.active", bid), msg_id="msg_fixed")
    first = await client.post("/webhooks/billing/dodo", content=raw, headers=headers)
    assert first.json()["outcome"] == "subscription.active:growth"
    assert await _plan(services, bid) == ("growth", "active")
    again = await client.post("/webhooks/billing/dodo", content=raw, headers=headers)
    assert again.json()["outcome"] == "duplicate"

    overview = (await founder.get(f"/api/v1/b/{bid}/billing")).json()
    assert overview["plan"] == "growth" and overview["can_manage"] is True

    # Upgrading an existing subscription changes it in place (prorated), no second checkout.
    change = respx.post(f"{DODO}/subscriptions/sub_1/change-plan").mock(return_value=httpx.Response(200, json={}))
    res = await founder.post(f"/api/v1/b/{bid}/billing/checkout", json={"plan": "scale"})
    assert res.json()["mode"] == "changed"
    assert orjson.loads(change.calls[0].request.content)["product_id"] == "pdt_scale"
    raw, headers = _signed(_event("subscription.plan_changed", bid, product="pdt_scale"))
    await client.post("/webhooks/billing/dodo", content=raw, headers=headers)
    assert (await _plan(services, bid))[0] == "scale"

    # Payment trouble keeps the plan but flags it; expiry returns to Free.
    raw, headers = _signed(_event("subscription.on_hold", bid, product="pdt_scale"))
    await client.post("/webhooks/billing/dodo", content=raw, headers=headers)
    assert await _plan(services, bid) == ("scale", "past_due")
    raw, headers = _signed(_event("subscription.expired", bid, product="pdt_scale"))
    await client.post("/webhooks/billing/dodo", content=raw, headers=headers)
    assert await _plan(services, bid) == ("free", "cancelled")


@respx.mock
async def test_cancel_at_period_end(services: Services, client: httpx.AsyncClient, founder: Any, dodo: None) -> None:
    business = await new_business(founder)
    raw, headers = _signed(_event("subscription.active", business["id"]))
    await client.post("/webhooks/billing/dodo", content=raw, headers=headers)
    patch = respx.patch(f"{DODO}/subscriptions/sub_1").mock(return_value=httpx.Response(200, json={}))
    res = await founder.post(f"/api/v1/b/{business['id']}/billing/cancel", json={})
    assert res.status_code == 200, res.text
    assert res.json()["cancel_at_period_end"] is True
    assert orjson.loads(patch.calls[0].request.content) == {"cancel_at_next_billing_date": True}
    # Cancelled-at-period-end webhook doesn't downgrade early; the expiry does.
    raw, headers = _signed(_event("subscription.cancelled", business["id"], cancel_at_next_billing_date=True))
    await client.post("/webhooks/billing/dodo", content=raw, headers=headers)
    assert (await _plan(services, business["id"]))[0] == "growth"


async def test_free_plan_cant_run_ads(services: Services, founder: Any) -> None:
    business = await new_business(founder)
    async with services.db.tenant(uuid.UUID(business["id"])) as s:
        b = await s.get(Business, uuid.UUID(business["id"]))
        assert b is not None
        b.plan = "growth"
    res = await founder.post(
        f"/api/v1/b/{business['id']}/gtm/ads/accounts/practice", json={"platform": "meta", "monthly_cap_minor": 1000}
    )
    assert res.status_code == 402
    assert res.json()["upgrade_to"] == "scale"


async def test_usage_nudge_once_at_80_percent(services: Services, founder: Any) -> None:
    business = await new_business(founder)
    bid = uuid.UUID(business["id"])
    async with services.db.tenant(bid) as s:
        b = await s.get(Business, bid)
        assert b is not None
        b.plan = "free"  # 10 posts a month
        for _ in range(9):
            await add_usage(s, bid, "posts_per_month")
        nudges = (
            (await s.execute(select(Notification).where(Notification.kind == "billing.usage_nudge"))).scalars().all()
        )
    assert len(nudges) == 1
    assert "8 of 10" in nudges[0].title


@respx.mock
async def test_ad_share_reported_in_usd_and_flagged_otherwise(services: Services, founder: Any, dodo: None) -> None:
    business = await new_business(founder, currency="USD")
    bid = uuid.UUID(business["id"])
    today = today_in("Asia/Kolkata")
    last_month = today.replace(day=1) - timedelta(days=1)
    ingest = respx.post(f"{DODO}/events/ingest").mock(return_value=httpx.Response(200, json={}))
    async with services.db.tenant(bid) as s:
        sub = await s.get(Subscription, bid)
        assert sub is not None
        sub.provider_customer_id = "cus_9"
        for currency, spend in (("USD", 100_000), ("INR", 500_000)):
            acct = AdAccount(
                business_id=bid,
                platform="meta",
                external_id=f"act_{currency}",
                name=currency,
                currency=currency,
                status="connected",
                monthly_cap_minor=10**7,
            )
            s.add(acct)
            await s.flush()
            ad = AdCampaign(business_id=bid, ad_account_id=acct.id, platform="meta", name="x", status="paused")
            s.add(ad)
            await s.flush()
            s.add(AdMetricDaily(business_id=bid, ad_campaign_id=ad.id, day=last_month, spend_minor=spend))
        await enqueue(s, "gtm.ads_share", {"period": last_month.strftime("%Y-%m")}, business_id=bid)
    await Worker(services).run_until_idle()
    sent = orjson.loads(ingest.calls[0].request.content)["events"][0]
    assert sent["customer_id"] == "cus_9"
    assert sent["metadata"]["fee_cents"] == 7_000  # 7% of $1,000
    assert len(ingest.calls) == 1, "INR isn't charged through the USD meter"
    async with services.db.tenant(bid) as s:
        flagged = (
            (await s.execute(select(Notification).where(Notification.kind == "billing.ad_share_manual")))
            .scalars()
            .all()
        )
    assert len(flagged) == 1 and "350.00 INR" in flagged[0].title  # 7% of ₹5,000


async def test_plan_limits_are_enforced_with_live_counts(services: Services, founder: Any) -> None:
    business = await new_business(founder)
    bid = business["id"]
    async with services.db.tenant(uuid.UUID(bid)) as s:
        b = await s.get(Business, uuid.UUID(bid))
        assert b is not None
        b.plan = "free"  # 1 platform, 250 contacts, 1 landing page, 1 seat
    gtm = f"/api/v1/b/{bid}/gtm"
    assert (await founder.post(f"{gtm}/channels/practice", json={"platform": "instagram"})).status_code == 201
    second_account_same_platform = await founder.post(
        f"{gtm}/channels/practice", json={"platform": "instagram", "followers": 5}
    )
    assert second_account_same_platform.status_code == 201, "a platform you already use doesn't count again"
    new_platform = await founder.post(f"{gtm}/channels/practice", json={"platform": "x"})
    assert new_platform.status_code == 402
    assert new_platform.json()["upgrade_to"] == "autopilot"

    pages = (await founder.get(f"{gtm}/landing-pages")).json()["items"]
    if not pages:
        assert (
            await founder.post(f"{gtm}/landing-pages", json={"title": "Join", "kind": "newsletter"})
        ).status_code == 201
    assert (
        await founder.post(f"{gtm}/landing-pages", json={"title": "Another", "kind": "waitlist"})
    ).status_code == 402
    assert (await founder.post(f"/api/v1/b/{bid}/members/invite", json={"email": "a@example.com"})).status_code == 402

    usage = (await founder.get(f"/api/v1/b/{bid}/usage")).json()["metrics"]
    assert usage["platforms"] == {"used": 1, "limit": 1}
    assert usage["landing_pages"]["used"] == 1
    assert usage["seats"]["used"] == 1


async def test_import_stops_at_the_contact_limit(services: Services, founder: Any) -> None:
    business = await new_business(founder)
    bid = business["id"]
    async with services.db.tenant(uuid.UUID(bid)) as s:
        b = await s.get(Business, uuid.UUID(bid))
        assert b is not None
        b.plan = "free"
    rows = "email,name\n" + "\n".join(f"p{i}@example.com,P{i}" for i in range(260))
    res = await founder.post(
        f"/api/v1/b/{bid}/gtm/contacts/import",
        files={"file": ("c.csv", rows.encode(), "text/csv")},
        data={"stage": "lead"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["created"] == 250 and body["over_plan_limit"] == 10
    assert (
        await founder.post(f"/api/v1/b/{bid}/gtm/contacts", json={"email": "one-more@example.com"})
    ).status_code == 402
