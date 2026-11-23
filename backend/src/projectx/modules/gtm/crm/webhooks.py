"""Payment webhooks from the business's own provider (Stripe, Razorpay or Dodo Payments).

Each business stores its webhook signing secret in the vault. Signatures are verified before
anything is parsed; replays outside a five-minute window are refused; recording is idempotent.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from typing import Any, Literal

import orjson
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from projectx.api.deps import OwnerDep
from projectx.modules.gtm.config import gtm_settings, update_gtm_settings
from projectx.modules.gtm.crm.payments import record_payment
from projectx.services import Services
from projectx.spine.ledger.service import Actor, append
from projectx.spine.tenancy.models import Business

public_router = APIRouter(include_in_schema=False)
router = APIRouter(prefix="/api/v1/b/{business_id}/gtm", tags=["gtm: payments"])

Provider = Literal["stripe", "razorpay", "dodo"]
TOLERANCE_S = 300


def verify_stripe(payload: bytes, header: str, secret: str, now: float | None = None) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    ts, sig = parts.get("t"), parts.get("v1")
    if not ts or not sig or abs((now or time.time()) - int(ts)) > TOLERANCE_S:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def verify_razorpay(payload: bytes, header: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def verify_standard_webhook(payload: bytes, headers: dict[str, str], secret: str, now: float | None = None) -> bool:
    """Standard Webhooks (used by Dodo Payments): HMAC-SHA256 over "id.timestamp.body"."""
    msg_id, ts, signatures = (
        headers.get("webhook-id"),
        headers.get("webhook-timestamp"),
        headers.get("webhook-signature"),
    )
    if not msg_id or not ts or not signatures or abs((now or time.time()) - int(ts)) > TOLERANCE_S:
        return False
    key = base64.b64decode(secret.removeprefix("whsec_"))
    expected = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + payload, hashlib.sha256).digest()).decode()
    return any(hmac.compare_digest(expected, s.split(",", 1)[-1]) for s in signatures.split())


def parse_event(provider: str, event: dict[str, Any]) -> dict[str, Any] | None:
    """Normalise a provider event into a payment, or None when the event isn't a payment."""
    kind = event.get("type") or event.get("event")
    if provider == "stripe":
        obj = event.get("data", {}).get("object", {})
        if kind == "checkout.session.completed" and obj.get("payment_status") == "paid":
            return {
                "external_id": obj.get("payment_intent") or obj["id"],
                "amount": obj.get("amount_total", 0),
                "currency": obj.get("currency", "usd"),
                "email": (obj.get("customer_details") or {}).get("email"),
                "kind": "subscription" if obj.get("mode") == "subscription" else "one_time",
            }
        if kind == "invoice.paid":
            return {
                "external_id": obj["id"],
                "amount": obj.get("amount_paid", 0),
                "currency": obj.get("currency", "usd"),
                "email": obj.get("customer_email"),
                "kind": "subscription",
            }
        if kind == "charge.refunded":
            return {
                "external_id": f"refund-{obj['id']}",
                "amount": -int(obj.get("amount_refunded", 0)),
                "currency": obj.get("currency", "usd"),
                "email": (obj.get("billing_details") or {}).get("email"),
                "kind": "refund",
            }
        return None
    if provider == "razorpay":
        entity = event.get("payload", {}).get("payment", {}).get("entity", {})
        if kind == "payment.captured":
            return {
                "external_id": entity["id"],
                "amount": entity.get("amount", 0),
                "currency": entity.get("currency", "INR"),
                "email": entity.get("email"),
                "kind": "one_time",
            }
        return None
    if provider == "dodo":
        data = event.get("data", {})
        if kind in ("payment.succeeded",):
            return {
                "external_id": data.get("payment_id") or data.get("id"),
                "amount": data.get("total_amount", 0),
                "currency": data.get("currency", "USD"),
                "email": (data.get("customer") or {}).get("email"),
                "kind": "subscription" if data.get("subscription_id") else "one_time",
            }
        return None
    return None


@public_router.post("/webhooks/payments/{provider}/{business_slug}")
async def payment_webhook(provider: str, business_slug: str, request: Request) -> JSONResponse:
    from sqlalchemy import select

    services: Services = request.app.state.services
    if provider not in ("stripe", "razorpay", "dodo"):
        return JSONResponse({"error": "unknown provider"}, status_code=404)
    payload = await request.body()
    async with services.db.session() as session:
        business = await session.scalar(select(Business).where(Business.slug == business_slug))
    if business is None:
        return JSONResponse({"error": "unknown business"}, status_code=404)
    secret_id = gtm_settings(business).get("payment_webhooks", {}).get(provider)
    if not secret_id:
        return JSONResponse({"error": "not configured"}, status_code=404)
    async with services.db.tenant(business.id) as session:
        secret = (
            await services.vault.borrow(
                session, business.id, uuid.UUID(secret_id), purpose=f"verify {provider} webhook", actor="webhook"
            )
        )["secret"]
    headers = {k.lower(): v for k, v in request.headers.items()}
    ok = (
        verify_stripe(payload, headers.get("stripe-signature", ""), secret)
        if provider == "stripe"
        else verify_razorpay(payload, headers.get("x-razorpay-signature", ""), secret)
        if provider == "razorpay"
        else verify_standard_webhook(payload, headers, secret)
    )
    if not ok:
        return JSONResponse({"error": "bad signature"}, status_code=401)
    parsed = parse_event(provider, orjson.loads(payload))
    if parsed is None:
        return JSONResponse({"ignored": True})
    async with services.db.tenant(business.id) as session:
        biz = await session.get(Business, business.id)
        assert biz is not None
        payment, created = await record_payment(
            session,
            biz,
            provider=provider,
            external_id=str(parsed["external_id"]),
            amount_minor=int(parsed["amount"]),
            currency=str(parsed["currency"]).upper(),
            email=parsed.get("email"),
            kind=parsed["kind"],
            actor=Actor.integration(provider),
        )
    return JSONResponse({"recorded": created, "payment_id": str(payment.id)})


class WebhookSecretIn(BaseModel):
    secret: str = Field(min_length=8, max_length=500)


@router.put("/payment-webhooks/{provider}")
async def set_payment_webhook(provider: Provider, body: WebhookSecretIn, ctx: OwnerDep) -> dict[str, Any]:
    vault = ctx.services.vault
    secret_id = await vault.put(
        ctx.session,
        ctx.business_id,
        kind="webhook_secret",
        label=f"payments:{provider}",
        secret={"secret": body.secret},
    )
    current = gtm_settings(ctx.business).get("payment_webhooks", {})
    update_gtm_settings(ctx.business, {"payment_webhooks": {**current, provider: str(secret_id)}})
    await append(
        ctx.session,
        business_id=ctx.business_id,
        type="spine.credential.changed",
        actor=Actor.user(ctx.user.id),
        data={"secret_id": str(secret_id), "kind": "webhook_secret", "change": f"set {provider}"},
    )
    return {
        "provider": provider,
        "webhook_url": f"{ctx.services.settings.base_url}/webhooks/payments/{provider}/{ctx.business.slug}",
    }


__all__ = ["parse_event", "verify_razorpay", "verify_standard_webhook", "verify_stripe"]
