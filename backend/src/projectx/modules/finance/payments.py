"""Getting paid by card: invoice pay links, subscription plans, and Stripe's events booked automatically.

- Every sent invoice carries a lasting pay link (/pay/<token>). Opening it creates a fresh Stripe Checkout for
  what is still owed, so a link never goes stale and never charges twice for a paid invoice.
- Plans (a name, a price, monthly or yearly) get a subscribe link (/subscribe/<token>).
- Stripe's events (own keys: /webhooks/stripe/<slug>; ours: /webhooks/stripe/connect) are verified, then recorded
  as payments; Finance's normal flow matches them to the invoice and books them. Subscription changes are tracked.
Tokens are HMAC-signed ids: unguessable, nothing personal inside, and they expire only when the thing is paid.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from datetime import UTC, datetime
from typing import Any

import orjson
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import ForeignKey, Index, String, select
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TenantMixin, TimestampMixin
from projectx.kernel.errors import NotConfigured, ValidationFailed
from projectx.services import Services
from projectx.spine.integrations import service as integrations
from projectx.spine.integrations.stripe import StripeFor
from projectx.spine.ledger.service import Actor
from projectx.spine.routing.models import RoutingKey
from projectx.spine.tenancy.models import Business
from projectx.spine.twin.models import Contact


class Plan(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "fin_plans"

    name: Mapped[str] = mapped_column(String(120))
    amount_minor: Mapped[int]
    currency: Mapped[str] = mapped_column(String(3))
    interval: Mapped[str] = mapped_column(String(5), default="month")  # month | year
    active: Mapped[bool] = mapped_column(default=True)


class CustomerSubscription(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "fin_subscriptions"
    __table_args__ = (Index("uq_fin_subscriptions_external", "business_id", "provider", "external_id", unique=True),)

    plan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("fin_plans.id", ondelete="SET NULL"))
    contact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    provider: Mapped[str] = mapped_column(String(20), default="stripe")
    external_id: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | past_due | canceled | …
    current_period_end: Mapped[datetime | None]


# ---------------------------------------------------------------- signed links


def _sign(secret: str, kind: str, business_id: uuid.UUID, item_id: uuid.UUID) -> str:
    raw = business_id.bytes + item_id.bytes
    mac = hmac.new(secret.encode(), kind.encode() + raw, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(raw + mac).decode().rstrip("=")


def _unsign(secret: str, kind: str, token: str) -> tuple[uuid.UUID, uuid.UUID] | None:
    try:
        data = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except ValueError:
        return None
    if len(data) != 48:
        return None
    raw, mac = data[:32], data[32:]
    if not hmac.compare_digest(mac, hmac.new(secret.encode(), kind.encode() + raw, hashlib.sha256).digest()[:16]):
        return None
    return uuid.UUID(bytes=raw[:16]), uuid.UUID(bytes=raw[16:])


def pay_url(services: Services, business_id: uuid.UUID, invoice_id: uuid.UUID) -> str:
    token = _sign(services.settings.secret_key.get_secret_value(), "pay", business_id, invoice_id)
    return f"{services.settings.base_url}/pay/{token}"


def subscribe_url(services: Services, business_id: uuid.UUID, plan_id: uuid.UUID) -> str:
    token = _sign(services.settings.secret_key.get_secret_value(), "sub", business_id, plan_id)
    return f"{services.settings.base_url}/subscribe/{token}"


public_router = APIRouter(include_in_schema=False)


@public_router.get("/pay/{token}")
async def pay(token: str, request: Request) -> RedirectResponse:
    from projectx.modules.finance.models import Invoice

    services: Services = request.app.state.services
    ids = _unsign(services.settings.secret_key.get_secret_value(), "pay", token)
    done = f"{services.settings.web_url}/paid"
    if ids is None:
        return RedirectResponse(f"{done}?error=invalid", status_code=303)
    business_id, invoice_id = ids
    async with services.db.tenant(business_id) as session:
        inv = await session.get(Invoice, invoice_id)
        if inv is None or inv.status in ("void", "draft"):
            return RedirectResponse(f"{done}?error=unavailable", status_code=303)
        if inv.status == "paid":
            return RedirectResponse(f"{done}?invoice={inv.number}", status_code=303)
        owed, number, currency = inv.total_minor - inv.paid_minor, inv.number, inv.currency
        contact = await session.get(Contact, inv.contact_id) if inv.contact_id else None
        email = contact.email if contact and not contact.erased_at else None
    stripe = await StripeFor.for_business(services, business_id)
    fee = owed * stripe.fee_bp // 10000
    session_obj = await stripe(
        "POST",
        "/checkout/sessions",
        {
            "mode": "payment",
            "line_items": [
                {
                    "quantity": 1,
                    "price_data": {
                        "currency": currency.lower(),
                        "unit_amount": owed,
                        "product_data": {"name": f"Invoice {number}"},
                    },
                }
            ],
            "customer_email": email,
            "metadata": {"invoice_id": str(invoice_id), "business_id": str(business_id)},
            "payment_intent_data": {"metadata": {"invoice_id": str(invoice_id)}, "application_fee_amount": fee or None},
            "success_url": f"{done}?invoice={number}",
            "cancel_url": f"{done}?invoice={number}&cancelled=1",
        },
        idempotency_key=f"pay:{invoice_id}:{owed}:{datetime.now(UTC):%Y%m%d%H%M}",
    )
    return RedirectResponse(str(session_obj["url"]), status_code=303)


@public_router.get("/subscribe/{token}")
async def subscribe(token: str, request: Request) -> RedirectResponse:
    services: Services = request.app.state.services
    ids = _unsign(services.settings.secret_key.get_secret_value(), "sub", token)
    done = f"{services.settings.web_url}/subscribed"
    if ids is None:
        return RedirectResponse(f"{done}?error=invalid", status_code=303)
    business_id, plan_id = ids
    async with services.db.tenant(business_id) as session:
        plan = await session.get(Plan, plan_id)
        if plan is None or not plan.active:
            return RedirectResponse(f"{done}?error=unavailable", status_code=303)
        name, amount, currency, interval = plan.name, plan.amount_minor, plan.currency, plan.interval
    stripe = await StripeFor.for_business(services, business_id)
    session_obj = await stripe(
        "POST",
        "/checkout/sessions",
        {
            "mode": "subscription",
            "line_items": [
                {
                    "quantity": 1,
                    "price_data": {
                        "currency": currency.lower(),
                        "unit_amount": amount,
                        "recurring": {"interval": interval},
                        "product_data": {"name": name},
                    },
                }
            ],
            "metadata": {"plan_id": str(plan_id), "business_id": str(business_id)},
            "subscription_data": {
                "metadata": {"plan_id": str(plan_id)},
                "application_fee_percent": (stripe.fee_bp / 100) or None,
            },
            "success_url": f"{done}?plan={plan_id}",
            "cancel_url": f"{done}?plan={plan_id}&cancelled=1",
        },
    )
    return RedirectResponse(str(session_obj["url"]), status_code=303)


# ---------------------------------------------------------------- Stripe events


@public_router.post("/webhooks/stripe/connect")
async def connect_webhook(request: Request) -> JSONResponse:
    from projectx.modules.gtm.crm.webhooks import verify_stripe

    services: Services = request.app.state.services
    secret = services.settings.stripe_connect_webhook_secret
    payload = await request.body()
    if secret is None or not verify_stripe(
        payload, request.headers.get("stripe-signature", ""), secret.get_secret_value()
    ):
        return JSONResponse({"error": "bad signature"}, status_code=401)
    event = orjson.loads(payload)
    async with services.db.session() as session:
        route = await session.get(RoutingKey, ("stripe_account", str(event.get("account", ""))))
    if route is None:
        return JSONResponse({"ignored": "unknown account"})
    return JSONResponse(await handle_event(services, route.business_id, event))


@public_router.post("/webhooks/stripe/{slug}")
async def own_webhook(slug: str, request: Request) -> JSONResponse:
    from projectx.modules.gtm.crm.webhooks import verify_stripe

    services: Services = request.app.state.services
    async with services.db.session() as session:
        business = await session.scalar(select(Business).where(Business.slug == slug))
    if business is None:
        return JSONResponse({"error": "unknown business"}, status_code=404)
    try:
        creds = await integrations.resolve(services, business.id, "stripe", purpose="verify stripe webhook")
    except NotConfigured:
        return JSONResponse({"error": "not configured"}, status_code=404)
    payload = await request.body()
    secret = creds.secrets.get("webhook_secret") if creds.mode == "own" else None
    if not secret or not verify_stripe(payload, request.headers.get("stripe-signature", ""), str(secret)):
        return JSONResponse({"error": "bad signature"}, status_code=401)
    return JSONResponse(await handle_event(services, business.id, orjson.loads(payload)))


async def handle_event(services: Services, business_id: uuid.UUID, event: dict[str, Any]) -> dict[str, Any]:
    from projectx.modules.finance.models import Invoice
    from projectx.modules.gtm.crm.payments import record_payment

    kind = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}
    actor = Actor.integration("stripe")
    async with services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        assert business is not None
        if kind == "account.updated":
            row = await integrations._row(session, "stripe", lock=True)
            if row is not None and row.mode == "platform" and row.settings.get("account_id") == obj.get("id"):
                row.status = "ok" if obj.get("charges_enabled") else "pending"
            return {"account": row.status if row else "unknown"}
        if (
            kind == "checkout.session.completed"
            and obj.get("payment_status") == "paid"
            and obj.get("mode") == "payment"
        ):
            meta = obj.get("metadata") or {}
            contact_id = None
            if meta.get("invoice_id"):
                inv = await session.get(Invoice, uuid.UUID(meta["invoice_id"]))
                contact_id = inv.contact_id if inv else None
            payment, created = await record_payment(
                session,
                business,
                provider="stripe",
                external_id=str(obj.get("payment_intent") or obj["id"]),
                amount_minor=int(obj.get("amount_total", 0)),
                currency=str(obj.get("currency", business.currency)).upper(),
                email=(obj.get("customer_details") or {}).get("email"),
                contact_id=contact_id,
                description="Invoice payment" if meta.get("invoice_id") else "Card payment",
                actor=actor,
                meta={"invoice_id": meta["invoice_id"]} if meta.get("invoice_id") else None,
            )
            return {"payment_id": str(payment.id), "recorded": created}
        if kind == "invoice.paid":  # subscription renewals and first payments
            sub_id = str(obj.get("subscription") or "")
            payment, created = await record_payment(
                session,
                business,
                provider="stripe",
                external_id=str(obj["id"]),
                amount_minor=int(obj.get("amount_paid", 0)),
                currency=str(obj.get("currency", business.currency)).upper(),
                email=obj.get("customer_email"),
                kind="subscription",
                description="Subscription",
                actor=actor,
                meta={"subscription": sub_id} if sub_id else None,
            )
            if sub_id:
                await _upsert_sub(session, business_id, sub_id, {"status": "active"}, payment.contact_id, obj)
            return {"payment_id": str(payment.id), "recorded": created}
        if kind in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
            status = "canceled" if kind.endswith("deleted") else str(obj.get("status", "active"))
            await _upsert_sub(session, business_id, str(obj["id"]), {"status": status}, None, obj)
            return {"subscription": status}
        if kind == "charge.refunded":
            payment, created = await record_payment(
                session,
                business,
                provider="stripe",
                external_id=f"refund-{obj['id']}",
                amount_minor=-int(obj.get("amount_refunded", 0)),
                currency=str(obj.get("currency", business.currency)).upper(),
                email=(obj.get("billing_details") or {}).get("email"),
                kind="refund",
                actor=actor,
            )
            return {"refund": str(payment.id), "recorded": created}
    return {"ignored": kind}


async def _upsert_sub(
    session: Any, business_id: uuid.UUID, external_id: str, values: dict[str, Any], contact_id: Any, obj: dict[str, Any]
) -> None:
    sub = await session.scalar(
        select(CustomerSubscription).where(
            CustomerSubscription.provider == "stripe", CustomerSubscription.external_id == external_id
        )
    )
    if sub is None:
        plan = (obj.get("metadata") or {}).get("plan_id") or (
            (obj.get("subscription_details") or {}).get("metadata") or {}
        ).get("plan_id")
        sub = CustomerSubscription(
            business_id=business_id,
            external_id=external_id,
            plan_id=uuid.UUID(plan) if plan else None,
            contact_id=contact_id,
        )
        session.add(sub)
    sub.status = values["status"]
    if contact_id and sub.contact_id is None:
        sub.contact_id = contact_id
    period_end = obj.get("current_period_end") or ((obj.get("lines") or {}).get("data") or [{}])[0].get(
        "period", {}
    ).get("end")
    if period_end:
        sub.current_period_end = datetime.fromtimestamp(int(period_end), UTC)
    await session.flush()


async def register_connect_account(session: Any, business_id: uuid.UUID, account_id: str) -> None:
    from sqlalchemy.dialects.postgresql import insert

    await session.execute(
        insert(RoutingKey)
        .values(namespace="stripe_account", key=account_id, business_id=business_id)
        .on_conflict_do_nothing()
    )


async def cancel_subscription(services: Services, business_id: uuid.UUID, sub: CustomerSubscription) -> None:
    if sub.status == "canceled":
        return
    if sub.provider != "stripe":
        raise ValidationFailed("Only Stripe subscriptions can be cancelled here")
    stripe = await StripeFor.for_business(services, business_id)
    await stripe("DELETE", f"/subscriptions/{sub.external_id}")
    sub.status = "canceled"
