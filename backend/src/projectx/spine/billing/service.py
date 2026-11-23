"""Charging our own customers through Dodo Payments (merchant of record, so the company needn't be registered
in every country it sells to).

The plan a business is on changes only from verified webhooks, never from the browser coming back from
checkout: the return page just says "we'll update your plan in a moment". Every webhook is recorded once
(`billing_events`), so retries and replays are harmless.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.errors import AppError, Conflict, ValidationFailed
from projectx.services import Services
from projectx.settings import Settings
from projectx.spine.billing.models import BillingEvent, Subscription
from projectx.spine.billing.plans import PLAN_ORDER, plan_for
from projectx.spine.ledger.service import Actor, append
from projectx.spine.notifications.service import notify
from projectx.spine.tenancy.models import Business

SYSTEM = Actor.system("billing")


class BillingUnavailable(AppError):
    status, code, title = 503, "billing_unavailable", "Billing isn't set up"


@dataclass
class DodoClient:
    http: httpx.AsyncClient
    api_key: str
    environment: str

    @property
    def base(self) -> str:
        return "https://live.dodopayments.com" if self.environment == "live_mode" else "https://test.dodopayments.com"

    async def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self.http.request(
            method, f"{self.base}{path}", json=body, headers={"Authorization": f"Bearer {self.api_key}"}
        )
        if response.status_code >= 400:
            try:
                message = response.json().get("message") or response.text[:300]
            except ValueError:
                message = response.text[:300]
            if response.status_code >= 500:
                raise BillingUnavailable(f"The payment provider is having trouble: {message}")
            raise ValidationFailed(f"The payment provider refused: {message}")
        return response.json() if response.content else {}

    async def create_checkout(
        self, *, product_id: str, email: str, name: str, return_url: str, metadata: dict[str, str]
    ) -> dict[str, Any]:
        return await self._call(
            "POST",
            "/checkouts",
            {
                "product_cart": [{"product_id": product_id, "quantity": 1}],
                "customer": {"email": email, "name": name},
                "return_url": return_url,
                "metadata": metadata,
            },
        )

    async def change_plan(self, subscription_id: str, product_id: str) -> dict[str, Any]:
        return await self._call(
            "POST",
            f"/subscriptions/{subscription_id}/change-plan",
            {"product_id": product_id, "quantity": 1, "proration_billing_mode": "prorated_immediately"},
        )

    async def cancel_at_period_end(self, subscription_id: str, cancel: bool = True) -> dict[str, Any]:
        return await self._call("PATCH", f"/subscriptions/{subscription_id}", {"cancel_at_next_billing_date": cancel})

    async def portal(self, customer_id: str) -> str:
        result = await self._call("POST", f"/customers/{customer_id}/customer-portal/session")
        return str(result["link"])

    async def ingest_usage(self, *, event_id: str, customer_id: str, event_name: str, metadata: dict[str, Any]) -> None:
        await self._call(
            "POST",
            "/events/ingest",
            {
                "events": [
                    {"event_id": event_id, "customer_id": customer_id, "event_name": event_name, "metadata": metadata}
                ]
            },
        )


def client(services: Services) -> DodoClient:
    key = services.settings.dodo_api_key
    if key is None:
        raise BillingUnavailable("Billing isn't configured on this server yet.")
    return DodoClient(services.http, key.get_secret_value(), services.settings.dodo_environment)


def product_for(settings: Settings, plan: str) -> str:
    product = settings.dodo_product_ids.get(plan)
    if not product:
        raise BillingUnavailable(f"The {plan_for(plan).name} plan isn't for sale on this server yet.")
    return product


def plan_for_product(settings: Settings, product_id: str | None) -> str | None:
    return next((plan for plan, pid in settings.dodo_product_ids.items() if pid == product_id), None)


async def subscription_of(session: AsyncSession, business_id: uuid.UUID) -> Subscription:
    sub = await session.get(Subscription, business_id)
    if sub is None:
        sub = Subscription(business_id=business_id, plan="free", status="active")
        session.add(sub)
        await session.flush()
    return sub


def billing_view(business: Business, sub: Subscription, settings: Settings) -> dict[str, Any]:
    plan = plan_for(business.plan)
    return {
        "plan": plan.key,
        "plan_name": plan.name,
        "status": sub.status,
        "provider": sub.provider,
        "current_period_end": sub.current_period_end.isoformat() if sub.current_period_end else None,
        "cancel_at_period_end": sub.cancel_at_period_end,
        "ad_spend_share_bps": plan.ad_spend_share_bps,
        "configured": settings.dodo_api_key is not None,
        "for_sale": [p for p in PLAN_ORDER if p in settings.dodo_product_ids],
        "can_manage": bool(sub.provider_customer_id),
    }


async def start_checkout(
    session: AsyncSession, services: Services, business: Business, *, plan: str, email: str, name: str
) -> dict[str, Any]:
    """New paid subscription → a hosted checkout page. Existing one → change plan in place (prorated)."""
    if plan not in PLAN_ORDER or plan == "free":
        raise ValidationFailed("Pick a paid plan; to go back to Free, cancel instead")
    if plan == business.plan:
        raise Conflict(f"You're already on {plan_for(plan).name}")
    sub = await subscription_of(session, business.id)
    dodo = client(services)
    product = product_for(services.settings, plan)
    if sub.provider_subscription_id and sub.status in ("active", "past_due"):
        await dodo.change_plan(sub.provider_subscription_id, product)
        await append(
            session,
            business_id=business.id,
            type="spine.billing.changed",
            actor=SYSTEM,
            data={"plan": plan, "status": "change_requested", "provider": "dodo"},
        )
        return {"mode": "changed", "url": None}
    checkout = await dodo.create_checkout(
        product_id=product,
        email=email,
        name=name,
        return_url=f"{services.settings.web_url}/b/{business.id}/settings?tab=plan&checkout=done",
        metadata={"business_id": str(business.id), "plan": plan},
    )
    return {"mode": "checkout", "url": checkout["checkout_url"]}


async def cancel(session: AsyncSession, services: Services, business: Business, *, undo: bool = False) -> Subscription:
    sub = await subscription_of(session, business.id)
    if not sub.provider_subscription_id:
        raise Conflict("There's no paid subscription to cancel")
    await client(services).cancel_at_period_end(sub.provider_subscription_id, cancel=not undo)
    sub.cancel_at_period_end = not undo
    await append(
        session,
        business_id=business.id,
        type="spine.billing.changed",
        actor=SYSTEM,
        data={"plan": business.plan, "status": "cancel_undone" if undo else "cancel_at_period_end", "provider": "dodo"},
    )
    return sub


# ---------------------------------------------------------------- webhooks

_ACTIVE = {"subscription.active", "subscription.renewed", "subscription.plan_changed", "subscription.updated"}
_TROUBLE = {"subscription.on_hold", "subscription.failed", "payment.failed"}
_ENDED = {"subscription.expired", "subscription.cancelled"}


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


async def record_once(
    session: AsyncSession, event_id: str, provider: str, kind: str, business_id: uuid.UUID | None = None
) -> bool:
    """True the first time we see this event id; False for retries and replays."""
    result = await session.execute(
        insert(BillingEvent)
        .values(event_id=event_id, provider=provider, type=kind[:80], business_id=business_id)
        .on_conflict_do_nothing(index_elements=[BillingEvent.event_id])
        .returning(BillingEvent.event_id)
    )
    return result.scalar() is not None


async def resolve_business(session: AsyncSession, event: dict[str, Any]) -> uuid.UUID | None:
    """Find whose event this is (subscriptions are readable without a tenant)."""
    data: dict[str, Any] = event.get("data") or {}
    candidate: uuid.UUID | None = None
    try:
        raw = (data.get("metadata") or {}).get("business_id")
        candidate = uuid.UUID(str(raw)) if raw else None
    except ValueError:
        candidate = None
    if candidate is not None:
        found: Subscription | None = await session.get(Subscription, candidate)
        if found is not None:
            return found.business_id
    if data.get("subscription_id"):
        match: uuid.UUID | None = await session.scalar(
            select(Subscription.business_id).where(Subscription.provider_subscription_id == data["subscription_id"])
        )
        return match
    return None


async def apply_dodo_event(
    session: AsyncSession, settings: Settings, event_id: str, event: dict[str, Any], business_id: uuid.UUID
) -> str:
    """Runs inside the business's tenant transaction: recording the event and applying it commit together."""
    kind = str(event.get("type", ""))
    data: dict[str, Any] = event.get("data") or {}
    if not await record_once(session, event_id, "dodo", kind, business_id):
        return "duplicate"
    if kind not in _ACTIVE | _TROUBLE | _ENDED:
        return "ignored"
    metadata = data.get("metadata") or {}
    sub = await subscription_of(session, business_id)
    business = await session.get(Business, business_id)
    if business is None:
        return "unknown_business"
    sub.provider = "dodo"
    sub.provider_subscription_id = data.get("subscription_id") or sub.provider_subscription_id
    sub.provider_customer_id = (data.get("customer") or {}).get("customer_id") or sub.provider_customer_id
    sub.current_period_end = _parse_time(data.get("next_billing_date")) or sub.current_period_end
    sub.current_period_start = _parse_time(data.get("previous_billing_date")) or sub.current_period_start
    if "cancel_at_next_billing_date" in data:
        sub.cancel_at_period_end = bool(data["cancel_at_next_billing_date"])
    before = business.plan
    if kind in _ACTIVE:
        plan = plan_for_product(settings, data.get("product_id")) or metadata.get("plan") or business.plan
        if plan not in PLAN_ORDER:
            return "unknown_product"
        business.plan, sub.plan, sub.status = plan, plan, "active"
        sub.ad_spend_share_bps = plan_for(plan).ad_spend_share_bps
    elif kind in _TROUBLE:
        sub.status = "past_due"
        await notify(
            session,
            business.id,
            kind="billing.payment_failed",
            level="critical",
            title="Your payment didn't go through",
            body="Update your card to keep your plan. Nothing is switched off yet.",
            link="/settings?tab=plan",
        )
    elif kind == "subscription.expired" or (kind == "subscription.cancelled" and not sub.cancel_at_period_end):
        business.plan, sub.plan, sub.status = "free", "free", "cancelled"
        sub.ad_spend_share_bps = 0
    await append(
        session,
        business_id=business.id,
        type="spine.billing.changed",
        actor=SYSTEM,
        data={"plan": business.plan, "status": sub.status, "provider": "dodo", "previous_plan": before},
    )
    if business.plan != before:
        await notify(
            session,
            business.id,
            kind="billing.plan_changed",
            title=f"You're on {plan_for(business.plan).name}",
            body="Your plan changed. Limits and features update right away.",
            link="/settings?tab=plan",
        )
    return f"{kind}:{business.plan}"
