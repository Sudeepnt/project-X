"""Tracked links → landing pages → contacts, with attribution, consent, double opt-in and unsubscribe."""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from typing import Any
from urllib.parse import parse_qs, urlsplit

import orjson
from sqlalchemy import select

from projectx.modules.gtm.crm.webhooks import parse_event, verify_razorpay, verify_standard_webhook, verify_stripe
from projectx.modules.gtm.growth.public import _form_token
from projectx.services import Services
from projectx.spine.jobs.worker import Worker
from projectx.spine.twin.models import Consent, Contact, Touch


def gtm(business: dict[str, Any], path: str) -> str:
    return f"/api/v1/b/{business['id']}/gtm{path}"


async def _published_page(founder: Any, business: dict[str, Any], **kw: Any) -> dict[str, Any]:
    page = (
        await founder.post(gtm(business, "/landing-pages"), json={"title": "Daily digest", "kind": "newsletter", **kw})
    ).json()
    res = await founder.post(gtm(business, f"/landing-pages/{page['id']}/publish"), json={})
    assert res.status_code == 200, res.text
    return dict(res.json())


def _form(services: Services, page_id: str, **fields: str) -> dict[str, str]:
    ts = int(time.time()) - 5
    token = _form_token(services.settings.secret_key.get_secret_value(), uuid.UUID(page_id), ts)
    return {"ts": str(ts), "tk": token, "v": "A", **fields}


async def test_link_click_attributes_the_signup(services: Services, founder: Any, business: dict[str, Any]) -> None:
    page = await _published_page(founder, business)
    page_path = urlsplit(page["public_url"]).path
    link = (
        await founder.post(
            gtm(business, "/links"),
            json={"destination": f"https://bought.example{page_path}", "platform": "instagram", "label": "Reel 1"},
        )
    ).json()
    client = founder.client
    res = await client.get(f"/l/{link['slug']}")
    assert res.status_code == 302
    query = parse_qs(urlsplit(res.headers["location"]).query)
    assert query["utm_source"] == ["instagram"] and "pxc" in query
    click_id = query["pxc"][0]

    view = await client.get(f"{page_path}?pxc={click_id}")
    assert view.status_code == 200 and "Subscribe free" in view.text
    assert "content-security-policy" in view.headers

    email = f"reader-{uuid.uuid4().hex[:6]}@example.com"
    submit = await client.post(
        page_path, data=_form(services, page["id"], email=email, name="Rae Reader", consent="on", pxc=click_id)
    )
    assert submit.status_code == 200, submit.text
    async with services.db.tenant(uuid.UUID(business["id"])) as s:
        contact = await s.scalar(select(Contact).where(Contact.email == email))
        assert contact is not None
        assert contact.stage == "subscriber"
        assert contact.first_touch and contact.first_touch["platform"] == "instagram"
        kinds = set((await s.execute(select(Touch.kind).where(Touch.contact_id == contact.id))).scalars())
        assert {"link_click", "signup"} <= kinds
        # Double opt-in: no consent until the confirmation link is clicked.
        consents = (await s.execute(select(Consent).where(Consent.contact_id == contact.id))).scalars().all()
        assert consents == []
    await Worker(services).run_until_idle()
    confirm = next(m for m in services.sent_emails if m["to"] == email)  # type: ignore[attr-defined]
    path = urlsplit(confirm["text"].split(": ", 1)[1].strip()).path
    assert (await client.get(path)).status_code == 200
    async with services.db.tenant(uuid.UUID(business["id"])) as s:
        contact = await s.scalar(select(Contact).where(Contact.email == email))
        assert contact is not None
        statuses = (await s.execute(select(Consent.status).where(Consent.contact_id == contact.id))).scalars().all()
    assert statuses == ["granted"]


async def test_bots_and_bad_input_are_ignored(services: Services, founder: Any, business: dict[str, Any]) -> None:
    page = await _published_page(founder, business, slug=f"p-{uuid.uuid4().hex[:6]}")
    path = urlsplit(page["public_url"]).path
    honeypot_email = f"bot-{uuid.uuid4().hex[:6]}@example.com"
    await founder.client.post(path, data=_form(services, page["id"], email=honeypot_email, website="http://spam"))
    too_fast = _form(services, page["id"], email=f"fast-{uuid.uuid4().hex[:6]}@example.com")
    too_fast["ts"] = str(int(time.time()))
    too_fast["tk"] = _form_token(
        services.settings.secret_key.get_secret_value(), uuid.UUID(page["id"]), int(too_fast["ts"])
    )
    await founder.client.post(path, data=too_fast)
    bad = await founder.client.post(path, data=_form(services, page["id"], email="nope"))
    assert bad.status_code == 422
    async with services.db.tenant(uuid.UUID(business["id"])) as s:
        found = (
            await s.execute(select(Contact.email).where(Contact.email.in_([honeypot_email, too_fast["email"]])))
        ).all()
    assert found == []


async def test_unsubscribe_one_click(services: Services, founder: Any, business: dict[str, Any]) -> None:
    from projectx.modules.gtm.nurture.sequences import unsubscribe_token

    contact = (
        await founder.post(
            gtm(business, "/contacts"),
            json={
                "email": f"u-{uuid.uuid4().hex[:6]}@example.com",
                "stage": "subscriber",
                "consent": {"channel": "email", "purpose": "newsletter", "evidence": "form"},
            },
        )
    ).json()
    token = unsubscribe_token(
        services.settings.secret_key.get_secret_value(), uuid.UUID(business["id"]), uuid.UUID(contact["id"])
    )
    page = await founder.client.get(f"/u/{token}")
    assert page.status_code == 200 and "Unsubscribe?" in page.text
    res = await founder.client.post(
        f"/u/{token}",
        content=b"List-Unsubscribe=One-Click",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert res.status_code == 200
    updated = (await founder.get(gtm(business, f"/contacts/{contact['id']}"))).json()
    assert updated["unsubscribed"] is True
    assert (await founder.client.post("/u/not-a-real-token")).status_code == 400


def test_webhook_signatures() -> None:
    body = orjson.dumps({"type": "checkout.session.completed"})
    now = time.time()
    ts = str(int(now))
    stripe_sig = hmac.new(b"whsec_test", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    assert verify_stripe(body, f"t={ts},v1={stripe_sig}", "whsec_test", now=now)
    assert not verify_stripe(body, f"t={ts},v1={stripe_sig}", "other", now=now)
    assert not verify_stripe(body, f"t={int(now) - 3600},v1={stripe_sig}", "whsec_test", now=now)
    razor = hmac.new(b"rzp", body, hashlib.sha256).hexdigest()
    assert verify_razorpay(body, razor, "rzp") and not verify_razorpay(body, razor, "x")
    import base64

    key = base64.b64encode(b"dodo-secret-key").decode()
    sig = base64.b64encode(
        hmac.new(b"dodo-secret-key", f"msg_1.{ts}.".encode() + body, hashlib.sha256).digest()
    ).decode()
    headers = {"webhook-id": "msg_1", "webhook-timestamp": ts, "webhook-signature": f"v1,{sig}"}
    assert verify_standard_webhook(body, headers, f"whsec_{key}", now=now)
    assert not verify_standard_webhook(body, {**headers, "webhook-id": "msg_2"}, f"whsec_{key}", now=now)


def test_payment_event_parsing() -> None:
    stripe = parse_event(
        "stripe",
        {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_1",
                    "payment_intent": "pi_1",
                    "payment_status": "paid",
                    "amount_total": 2900,
                    "currency": "usd",
                    "customer_details": {"email": "a@b.co"},
                    "mode": "payment",
                }
            },
        },
    )
    assert stripe == {"external_id": "pi_1", "amount": 2900, "currency": "usd", "email": "a@b.co", "kind": "one_time"}
    razor = parse_event(
        "razorpay",
        {
            "event": "payment.captured",
            "payload": {"payment": {"entity": {"id": "pay_1", "amount": 49900, "currency": "INR", "email": "x@y.in"}}},
        },
    )
    assert razor and razor["amount"] == 49900
    assert parse_event("stripe", {"type": "customer.created", "data": {"object": {}}}) is None
