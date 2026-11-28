"""Bring your own keys or use ours: Stripe (own keys and Connect), pay and subscribe links, Stripe events booked
by Finance, and a business's own model server and mail server."""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import time
import uuid
from typing import Any
from urllib.parse import parse_qs

import httpx
import orjson
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import select

from projectx.modules.finance import service as fin
from projectx.modules.finance.models import Invoice
from projectx.modules.finance.payments import CustomerSubscription, pay_url
from projectx.services import Services
from projectx.spine.gateway.contracts import ModelRequest
from projectx.spine.integrations import catalog
from projectx.spine.integrations import service as integrations
from projectx.spine.jobs.worker import Worker
from projectx.spine.tenancy.models import Business

STRIPE = "https://api.stripe.com/v1"
WHSEC = "whsec_test_own"


def url(business: dict[str, Any], path: str) -> str:
    return f"/api/v1/b/{business['id']}{path}"


def signed(body: dict[str, Any], secret: str) -> tuple[bytes, dict[str, str]]:
    raw = orjson.dumps(body)
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    return raw, {"stripe-signature": f"t={ts},v1={sig}", "content-type": "application/json"}


@pytest.fixture
def platform_stripe(services: Services, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services.settings, "stripe_secret_key", SecretStr("sk_test_platform"))
    monkeypatch.setattr(services.settings, "stripe_connect_webhook_secret", SecretStr("whsec_platform"))
    monkeypatch.setattr(services.settings, "stripe_application_fee_bp", 200)


async def _own_stripe(founder: Any, business: dict[str, Any]) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{STRIPE}/balance").mock(return_value=httpx.Response(200, json={"object": "balance"}))
        res = await founder.put(
            url(business, "/integrations/stripe"),
            json={"values": {"secret_key": "sk_test_own_1234", "webhook_secret": WHSEC}},
        )
    assert res.status_code == 200, res.text


async def _sent_invoice(services: Services, business: dict[str, Any], amount: int = 50_000) -> Invoice:
    bid = uuid.UUID(business["id"])
    async with services.db.tenant(bid) as s:
        biz = await s.get(Business, bid)
        assert biz is not None
        inv, _ = await fin.create_invoice(
            s, biz, source_key=f"t:{uuid.uuid4()}", lines=[{"description": "Ad slot", "unit_minor": amount}]
        )
        await fin.issue(s, biz, inv, actor=fin.AGENT)
        s.expunge(inv)
        return inv


async def test_own_keys_are_tested_sealed_and_never_shown(
    services: Services, founder: Any, business: dict[str, Any]
) -> None:
    tools = (await founder.get(url(business, "/integrations"))).json()["items"]
    assert {"stripe", "razorpay", "smtp", "openai_compat", "anthropic"} <= {t["tool"] for t in tools}
    with respx.mock() as mock:
        mock.get(f"{STRIPE}/balance").mock(
            return_value=httpx.Response(401, json={"error": {"message": "Invalid API Key"}})
        )
        bad = await founder.put(
            url(business, "/integrations/stripe"),
            json={"values": {"secret_key": "sk_test_wrong", "webhook_secret": WHSEC}},
        )
    assert bad.status_code == 422 and bad.json()["code"] == "integration_test_failed"
    assert (
        await founder.put(url(business, "/integrations/stripe"), json={"values": {"secret_key": "sk_x", "nope": "1"}})
    ).status_code == 422

    await _own_stripe(founder, business)
    listed = (await founder.get(url(business, "/integrations"))).json()["items"]
    stripe = next(t for t in listed if t["tool"] == "stripe")
    assert stripe["mode"] == "own" and stripe["status"] == "ok" and stripe["settings"]["secret_key_hint"] == "…1234"
    assert stripe["webhook_url"].endswith(f"/webhooks/stripe/{business['slug']}")
    assert "sk_test_own_1234" not in json.dumps(listed) and WHSEC not in json.dumps(listed)
    creds = await integrations.resolve(services, uuid.UUID(business["id"]), "stripe")
    assert creds.mode == "own" and creds.secrets["secret_key"] == "sk_test_own_1234"

    assert (await founder.delete(url(business, "/integrations/stripe"))).status_code == 204
    assert not await integrations.is_ready(services, uuid.UUID(business["id"]), "stripe")


async def test_invoice_pay_link_and_stripe_event_mark_it_paid(
    services: Services, client: httpx.AsyncClient, founder: Any, business: dict[str, Any]
) -> None:
    bid = uuid.UUID(business["id"])
    await _own_stripe(founder, business)
    inv = await _sent_invoice(services, business)
    link = pay_url(services, bid, inv.id)
    with respx.mock() as mock:
        checkout = mock.post(f"{STRIPE}/checkout/sessions").mock(
            return_value=httpx.Response(200, json={"id": "cs_1", "url": "https://checkout.stripe.com/c/cs_1"})
        )
        res = await client.get(link.replace(services.settings.base_url, ""), follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"] == "https://checkout.stripe.com/c/cs_1"
    sent = parse_qs(checkout.calls.last.request.content.decode())
    assert sent["line_items[0][price_data][unit_amount]"] == [str(inv.total_minor)]
    assert sent["metadata[invoice_id]"] == [str(inv.id)]
    assert "Stripe-Account" not in checkout.calls.last.request.headers  # own keys: the business's own account
    assert "application_fee_amount" not in str(sent)  # no platform fee on own keys

    forged = await client.get(link.replace(services.settings.base_url, "")[:-3] + "AAA", follow_redirects=False)
    assert "error=invalid" in forged.headers["location"]

    event = {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_1",
                "mode": "payment",
                "payment_status": "paid",
                "payment_intent": "pi_9",
                "amount_total": inv.total_minor,
                "currency": inv.currency.lower(),
                "metadata": {"invoice_id": str(inv.id)},
            }
        },
    }
    raw, headers = signed(event, WHSEC)
    bad = await client.post(
        f"/webhooks/stripe/{business['slug']}", content=raw, headers={**headers, "stripe-signature": "t=1,v1=00"}
    )
    assert bad.status_code == 401
    ok = await client.post(f"/webhooks/stripe/{business['slug']}", content=raw, headers=headers)
    assert ok.status_code == 200 and ok.json()["recorded"] is True
    again = await client.post(f"/webhooks/stripe/{business['slug']}", content=raw, headers=headers)
    assert again.json()["recorded"] is False  # Stripe retries never double-count
    await Worker(services).run_until_idle()
    async with services.db.tenant(bid) as s:
        paid = await s.get(Invoice, inv.id)
    assert paid is not None and paid.status == "paid"  # matched to that invoice, even with no customer on file


async def test_use_ours_stripe_connect(
    services: Services, client: httpx.AsyncClient, founder: Any, business: dict[str, Any], platform_stripe: None
) -> None:
    bid = uuid.UUID(business["id"])
    with respx.mock() as mock:
        mock.post(f"{STRIPE}/accounts").mock(return_value=httpx.Response(200, json={"id": "acct_123"}))
        mock.post(f"{STRIPE}/account_links").mock(
            return_value=httpx.Response(200, json={"url": "https://connect.stripe.com/setup/x"})
        )
        res = await founder.post(url(business, "/integrations/stripe/platform"))
    assert res.json() == {
        "tool": "stripe",
        "mode": "platform",
        "status": "pending",
        "onboarding_url": "https://connect.stripe.com/setup/x",
    }
    assert not await integrations.is_ready(services, bid, "stripe")  # can't take money until onboarding is done

    raw, headers = signed(
        {
            "type": "account.updated",
            "account": "acct_123",
            "data": {"object": {"id": "acct_123", "charges_enabled": True}},
        },
        "whsec_platform",
    )
    assert (await client.post("/webhooks/stripe/connect", content=raw, headers=headers)).json() == {"account": "ok"}
    assert await integrations.is_ready(services, bid, "stripe")

    inv = await _sent_invoice(services, business, amount=10_000)
    with respx.mock() as mock:
        checkout = mock.post(f"{STRIPE}/checkout/sessions").mock(
            return_value=httpx.Response(200, json={"id": "cs_2", "url": "https://checkout.stripe.com/c/cs_2"})
        )
        await client.get(pay_url(services, bid, inv.id).replace(services.settings.base_url, ""), follow_redirects=False)
    req = checkout.calls.last.request
    assert req.headers["Stripe-Account"] == "acct_123"  # money goes to the founder's own connected account
    assert req.headers["Authorization"] == "Bearer sk_test_platform"
    assert parse_qs(req.content.decode())["payment_intent_data[application_fee_amount]"] == [
        str(inv.total_minor * 200 // 10000)
    ]


async def test_plans_subscribe_and_track_subscriptions(
    services: Services, client: httpx.AsyncClient, founder: Any, business: dict[str, Any]
) -> None:
    await _own_stripe(founder, business)
    plan = (
        await founder.post(
            url(business, "/finance/plans"), json={"name": "Pro", "amount_minor": 99_900, "interval": "month"}
        )
    ).json()
    with respx.mock() as mock:
        checkout = mock.post(f"{STRIPE}/checkout/sessions").mock(
            return_value=httpx.Response(200, json={"id": "cs_s", "url": "https://checkout.stripe.com/c/cs_s"})
        )
        res = await client.get(plan["subscribe_url"].replace(services.settings.base_url, ""), follow_redirects=False)
    assert res.status_code == 303
    sent = parse_qs(checkout.calls.last.request.content.decode())
    assert sent["mode"] == ["subscription"] and sent["line_items[0][price_data][recurring][interval]"] == ["month"]

    paid = {
        "type": "invoice.paid",
        "data": {
            "object": {
                "id": "in_1",
                "subscription": "sub_1",
                "amount_paid": 99_900,
                "currency": "inr",
                "customer_email": "subscriber@example.com",
                "subscription_details": {"metadata": {"plan_id": plan["id"]}},
                "lines": {"data": [{"period": {"end": 1893456000}}]},
            }
        },
    }
    raw, headers = signed(paid, WHSEC)
    assert (await client.post(f"/webhooks/stripe/{business['slug']}", content=raw, headers=headers)).json()[
        "recorded"
    ] is True
    subs = (await founder.get(url(business, "/finance/subscriptions"))).json()["items"]
    assert [(s["status"], s["plan_id"]) for s in subs] == [("active", plan["id"])] and subs[0]["contact_id"]

    raw, headers = signed(
        {"type": "customer.subscription.deleted", "data": {"object": {"id": "sub_1", "status": "canceled"}}}, WHSEC
    )
    await client.post(f"/webhooks/stripe/{business['slug']}", content=raw, headers=headers)
    async with services.db.tenant(uuid.UUID(business["id"])) as s:
        sub = (await s.execute(select(CustomerSubscription))).scalar_one()
    assert sub.status == "canceled"


async def test_a_business_can_bring_its_own_models_and_claude_stays_for_hard_jobs(
    services: Services, founder: Any, business: dict[str, Any]
) -> None:
    bid = uuid.UUID(business["id"])
    with respx.mock() as mock:
        mock.get("https://models.example.com/v1/models").mock(return_value=httpx.Response(200, json={"data": []}))
        mock.get("https://api.anthropic.com/v1/models").mock(return_value=httpx.Response(200, json={"data": []}))
        r1 = await founder.put(
            url(business, "/integrations/openai_compat"),
            json={
                "values": {
                    "base_url": "https://models.example.com/v1",
                    "api_key": "k-1",
                    "small": "qwen-small",
                    "frontier": "qwen-large",
                }
            },
        )
        r2 = await founder.put(url(business, "/integrations/anthropic"), json={"values": {"api_key": "sk-ant-own"}})
    assert r1.status_code == 200 and r2.status_code == 200
    route = await services.gateway._route(bid)
    assert route is not None
    normal = services.gateway._chain(
        ModelRequest(task="t", system="", messages=[], rung="small", max_rung="frontier"), route
    )
    assert [(p, m) for _, p, m in normal] == [("own_openai", "qwen-small"), ("own_openai", "qwen-large")]
    hard = services.gateway._chain(ModelRequest(task="t", system="", messages=[], hard=True), route)
    assert hard[0][1:] == ("own_anthropic", "claude-opus-5-5")
    services.gateway.forget_route(bid)


async def test_a_business_can_send_from_its_own_mail_server(
    services: Services, founder: Any, business: dict[str, Any]
) -> None:
    tool = catalog.get_tool("smtp")
    assert tool is not None
    catalog.register_tool(dataclasses.replace(tool, test=None))  # no real mail server in tests
    try:
        res = await founder.put(
            url(business, "/integrations/smtp"),
            json={
                "values": {
                    "host": "smtp.example.com",
                    "port": "587",
                    "username": "me",
                    "password": "pw",
                    "from_address": "Acme <hi@acme.example>",
                }
            },
        )
        assert res.status_code == 200, res.text
        server = await integrations.mail_server(services, uuid.UUID(business["id"]))
        assert server is not None and server["host"] == "smtp.example.com" and server["password"] == "pw"
        assert await integrations.mail_server(services, None) is None
    finally:
        catalog.register_tool(tool)
