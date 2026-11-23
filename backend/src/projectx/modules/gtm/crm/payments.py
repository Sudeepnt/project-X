"""Payments from the business's own payment provider (Stripe, Razorpay, Dodo or manual).

A payment moves its contact to Customer, adds to lifetime value, and is attributed to a campaign
through a promo code or the contact's most recent campaign. Recording is idempotent per
provider and external id, so webhook retries never double-count revenue.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.time import utcnow
from projectx.modules.gtm.crm.lifecycle import Attribution, Cause, change, record_touch, upsert_contact
from projectx.modules.gtm.models import PromoCode
from projectx.spine.ledger.service import Actor, append
from projectx.spine.tenancy.models import Business
from projectx.spine.twin.models import Contact, Payment


async def record_payment(
    session: AsyncSession,
    business: Business,
    *,
    provider: str,
    external_id: str,
    amount_minor: int,
    currency: str,
    email: str | None = None,
    name: str | None = None,
    contact_id: uuid.UUID | None = None,
    kind: str = "one_time",
    paid_at: datetime | None = None,
    promo_code: str | None = None,
    description: str | None = None,
    actor: Actor | None = None,
    meta: dict[str, Any] | None = None,
) -> tuple[Payment, bool]:
    actor = actor or Actor.integration(provider)
    existing = await session.scalar(
        select(Payment).where(
            Payment.business_id == business.id, Payment.provider == provider, Payment.external_id == external_id
        )
    )
    if existing is not None:
        return existing, False
    contact: Contact | None = await session.get(Contact, contact_id) if contact_id else None
    campaign_id: uuid.UUID | None = None
    code = None
    if promo_code:
        code = await session.scalar(
            select(PromoCode).where(PromoCode.business_id == business.id, PromoCode.code == promo_code.upper())
        )
        if code is not None:
            code.uses += 1
            campaign_id = code.campaign_id
    if contact is None and email:
        contact, _ = await upsert_contact(
            session,
            business,
            source="payment",
            email=email,
            name=name,
            stage="customer",
            cause=Cause("payment", actor=actor),
            attribution=Attribution(campaign_id=campaign_id) if campaign_id else None,
        )
    if campaign_id is None and contact is not None:
        campaign_id = contact.last_campaign_id
    refund = kind == "refund" or amount_minor < 0
    payment = Payment(
        business_id=business.id,
        contact_id=contact.id if contact else None,
        provider=provider,
        external_id=external_id[:200],
        amount_minor=amount_minor,
        currency=currency.upper(),
        kind=kind,
        status="refunded" if refund else "succeeded",
        paid_at=paid_at or utcnow(),
        promo_code=code.code if code else promo_code,
        campaign_id=campaign_id,
        description=description,
        meta=meta or {},
    )
    session.add(payment)
    await session.flush()
    if contact is not None:
        contact.lifetime_value_minor = (contact.lifetime_value_minor or 0) + amount_minor
        if not refund:
            contact.customer_type = contact.customer_type or ("subscription" if kind == "subscription" else "one_time")
            await record_touch(
                session,
                business,
                contact,
                kind="payment",
                campaign_id=campaign_id,
                data={"payment_id": str(payment.id)},
                cause=Cause("payment", payment.id, actor=actor),
            )
            await change(
                session, contact, cause=Cause("payment", payment.id, actor=actor), stage="customer", status="won"
            )
    await append(
        session,
        business_id=business.id,
        type="gtm.payment.recorded",
        actor=actor,
        subject=("payment", payment.id),
        data={
            "payment_id": str(payment.id),
            "contact_id": str(contact.id) if contact else None,
            "amount_minor": amount_minor,
            "currency": payment.currency,
            "kind": kind,
            "campaign_id": str(campaign_id) if campaign_id else None,
        },
    )
    return payment, True


def payment_view(p: Payment) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "contact_id": str(p.contact_id) if p.contact_id else None,
        "provider": p.provider,
        "external_id": p.external_id,
        "amount_minor": p.amount_minor,
        "currency": p.currency,
        "kind": p.kind,
        "status": p.status,
        "paid_at": p.paid_at.isoformat(),
        "promo_code": p.promo_code,
        "campaign_id": str(p.campaign_id) if p.campaign_id else None,
        "description": p.description,
    }
