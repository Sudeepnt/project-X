"""Creating businesses and checking membership."""

from __future__ import annotations

import re
import unicodedata
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.db.database import set_tenant
from projectx.kernel.errors import ValidationFailed
from projectx.kernel.ids import short_code
from projectx.kernel.time import is_valid_timezone
from projectx.spine.billing.models import Subscription
from projectx.spine.identity.models import User
from projectx.spine.ledger.service import Actor, append
from projectx.spine.modules import modules
from projectx.spine.policy.service import get_policy
from projectx.spine.tenancy.models import BUSINESS_MODELS, ROLE_RANK, Business, Membership

_RESERVED_SLUGS = frozenset({"api", "admin", "app", "www", "static", "assets", "login", "signup", "p", "l", "u"})


def slugify(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    return slug[:48] or "business"


async def unique_slug(session: AsyncSession, name: str) -> str:
    base = slugify(name)
    candidate = base if base not in _RESERVED_SLUGS else f"{base}-co"
    for _ in range(8):
        taken = await session.scalar(select(Business.id).where(Business.slug == candidate))
        if taken is None:
            return candidate
        candidate = f"{base}-{short_code(4).lower()}"
    return f"{base}-{uuid.uuid4().hex[:8]}"


async def create_business(
    session: AsyncSession,
    *,
    owner: User,
    name: str,
    website: str | None = None,
    industry: str | None = None,
    business_model: str = "other",
    country: str | None = None,
    timezone: str = "UTC",
    currency: str = "USD",
    description: str | None = None,
    practice_mode: bool = False,
    plan: str = "free",
) -> Business:
    if business_model not in BUSINESS_MODELS:
        raise ValidationFailed("Unknown business model", extra={"field": "business_model"})
    if not is_valid_timezone(timezone):
        raise ValidationFailed("Unknown timezone", extra={"field": "timezone"})
    business = Business(
        name=name.strip()[:120],
        slug=await unique_slug(session, name),
        website=website,
        industry=industry,
        business_model=business_model,
        country=(country or None),
        timezone=timezone,
        currency=currency.upper(),
        description=description,
        practice_mode=practice_mode,
        plan=plan,
        settings={},
        onboarding={"step": "brand"},
    )
    session.add(business)
    await session.flush()
    session.add(Membership(user_id=owner.id, business_id=business.id, role="owner"))
    session.add(Subscription(business_id=business.id, plan=plan, status="active"))
    await set_tenant(session, business.id)
    await get_policy(session, business.id)
    await append(
        session,
        business_id=business.id,
        type="spine.business.created",
        actor=Actor.user(owner.id),
        data={"plan": plan, "business_model": business_model},
    )
    await append(
        session,
        business_id=business.id,
        type="spine.member.added",
        actor=Actor.user(owner.id),
        data={"user_id": str(owner.id), "role": "owner"},
    )
    for module in modules():
        if module.on_business_created is not None:
            await module.on_business_created(session, business)
    return business


async def get_membership(session: AsyncSession, user_id: uuid.UUID, business_id: uuid.UUID) -> Membership | None:
    return await session.get(Membership, (user_id, business_id))


async def list_businesses(session: AsyncSession, user_id: uuid.UUID) -> list[tuple[Business, str]]:
    rows = await session.execute(
        select(Business, Membership.role)
        .join(Membership, Membership.business_id == Business.id)
        .where(Membership.user_id == user_id, Business.status != "deleted")
        .order_by(Business.created_at)
    )
    return [(b, role) for b, role in rows.all()]


def role_at_least(role: str, minimum: str) -> bool:
    return ROLE_RANK.get(role, -1) >= ROLE_RANK[minimum]


def business_view(b: Business, role: str | None = None) -> dict[str, Any]:
    view: dict[str, Any] = {
        "id": str(b.id),
        "name": b.name,
        "slug": b.slug,
        "website": b.website,
        "industry": b.industry,
        "business_model": b.business_model,
        "description": b.description,
        "country": b.country,
        "timezone": b.timezone,
        "currency": b.currency,
        "plan": b.plan,
        "status": b.status,
        "practice_mode": b.practice_mode,
        "onboarding": b.onboarding,
        "settings": b.settings,
    }
    if role is not None:
        view["role"] = role
    return view
