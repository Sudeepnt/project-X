"""Landing pages with A/B variants, consent capture and double opt-in."""

from __future__ import annotations

import base64
import hashlib
import hmac
import random
import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.errors import Conflict, ValidationFailed
from projectx.kernel.time import utcnow
from projectx.modules.gtm.models import LandingPage, LandingVariant, PageEvent
from projectx.spine.routing.models import RoutingKey
from projectx.spine.tenancy.models import Business
from projectx.spine.tenancy.service import slugify

# Which funnel stage a submission enters, by page kind.
STAGE_FOR_KIND = {
    "newsletter": "subscriber",
    "waitlist": "subscriber",
    "lead_magnet": "lead",
    "webinar": "lead",
    "consult": "sql",
    "media_kit": "lead",
    "generic": "lead",
}

# The touch a submission records, which sets the funnel stage it implies (see crm.lifecycle).
TOUCH_FOR_KIND = {
    "newsletter": "signup",
    "waitlist": "signup",
    "lead_magnet": "download",
    "media_kit": "download",
    "webinar": "webinar_register",
    "consult": "price_request",
    "generic": "form_submit",
}

TEMPLATES: dict[str, dict[str, Any]] = {
    "newsletter": {
        "headline": "The news that matters, in five minutes a day",
        "subheadline": "Join readers who start their morning with us.",
        "cta_label": "Subscribe free",
        "bullets": ["One email a day", "No spam, unsubscribe any time"],
        "thank_you": "You're in. Check your inbox to confirm.",
    },
    "lead_magnet": {
        "headline": "Get the free guide",
        "subheadline": "Practical, specific and short.",
        "cta_label": "Send me the guide",
        "bullets": ["Takes 10 minutes to read", "Use it today"],
        "thank_you": "On its way. Check your inbox.",
    },
    "consult": {
        "headline": "Book a free consultation",
        "subheadline": "30 minutes, a clear plan, no obligation.",
        "cta_label": "Request a call",
        "bullets": ["We reply within one business day"],
        "thank_you": "Thanks. We'll be in touch within one business day.",
    },
    "waitlist": {
        "headline": "Be first in line",
        "subheadline": "Join the waitlist for early access.",
        "cta_label": "Join the waitlist",
        "bullets": [],
        "thank_you": "You're on the list.",
    },
    "webinar": {
        "headline": "Join the live session",
        "subheadline": "Live Q&A at the end.",
        "cta_label": "Save my seat",
        "bullets": [],
        "thank_you": "Your seat is saved. We'll send a reminder.",
    },
    "media_kit": {
        "headline": "Advertise to our audience",
        "subheadline": "Get the media kit with rates and audience data.",
        "cta_label": "Get the media kit",
        "bullets": [],
        "thank_you": "The media kit is on its way.",
    },
    "generic": {
        "headline": "Let's talk",
        "subheadline": "",
        "cta_label": "Send",
        "bullets": [],
        "thank_you": "Thank you.",
    },
}

DEFAULT_CONSENT_TEXT = "I agree to receive emails from {business}. I can unsubscribe at any time."


async def create_page(
    session: AsyncSession,
    business: Business,
    *,
    title: str,
    kind: str = "lead_magnet",
    slug: str | None = None,
    campaign_id: uuid.UUID | None = None,
    content: dict[str, Any] | None = None,
) -> LandingPage:
    if kind not in TEMPLATES:
        raise ValidationFailed("Unknown page kind")
    page_slug = slugify(slug or title)[:80]
    taken = await session.scalar(
        select(LandingPage.id).where(LandingPage.business_id == business.id, LandingPage.slug == page_slug)
    )
    if taken is not None:
        raise Conflict("A page with this address already exists", extra={"field": "slug"})
    page = LandingPage(
        business_id=business.id,
        slug=page_slug,
        title=title[:200],
        kind=kind,
        status="draft",
        campaign_id=campaign_id,
        settings={
            "double_opt_in": kind in ("newsletter", "waitlist"),
            "consent_text": DEFAULT_CONSENT_TEXT.format(business=business.name),
            "stage_on_submit": STAGE_FOR_KIND[kind],
            "fields": ["email", "name"],
        },
    )
    session.add(page)
    await session.flush()
    session.add(
        LandingVariant(
            business_id=business.id, page_id=page.id, key="A", content={**TEMPLATES[kind], **(content or {})}
        )
    )
    await session.flush()
    return page


async def publish_page(session: AsyncSession, business: Business, page: LandingPage) -> LandingPage:
    page.status = "published"
    page.published_at = page.published_at or utcnow()
    await session.execute(
        insert(RoutingKey)
        .values(namespace="page", key=f"{business.slug}/{page.slug}", business_id=business.id, target_id=page.id)
        .on_conflict_do_nothing()
    )
    return page


def choose_variant(variants: list[LandingVariant], sticky_key: str | None) -> LandingVariant:
    """Thompson sampling on conversion rate; returning visitors keep the variant they saw."""
    if sticky_key:
        for v in variants:
            if v.key == sticky_key:
                return v
    if len(variants) == 1:
        return variants[0]
    best, best_draw = variants[0], -1.0
    for v in variants:
        draw = random.betavariate(1 + v.conversions, 1 + max(v.views - v.conversions, 0)) * (v.weight or 1.0)
        if draw > best_draw:
            best, best_draw = v, draw
    return best


async def record_view(
    session: AsyncSession, page: LandingPage, variant: LandingVariant, *, click_id: str | None, visitor: str | None
) -> None:
    session.add(
        PageEvent(
            business_id=page.business_id,
            page_id=page.id,
            variant_key=variant.key,
            kind="view",
            click_id=click_id,
            visitor_hash=visitor,
        )
    )
    await session.execute(update(LandingPage).where(LandingPage.id == page.id).values(views=LandingPage.views + 1))
    await session.execute(
        update(LandingVariant).where(LandingVariant.id == variant.id).values(views=LandingVariant.views + 1)
    )


async def record_submit(
    session: AsyncSession, page: LandingPage, variant_key: str | None, *, click_id: str | None, visitor: str | None
) -> None:
    session.add(
        PageEvent(
            business_id=page.business_id,
            page_id=page.id,
            variant_key=variant_key,
            kind="submit",
            click_id=click_id,
            visitor_hash=visitor,
        )
    )
    await session.execute(
        update(LandingPage).where(LandingPage.id == page.id).values(submissions=LandingPage.submissions + 1)
    )
    if variant_key:
        await session.execute(
            update(LandingVariant)
            .where(LandingVariant.page_id == page.id, LandingVariant.key == variant_key)
            .values(conversions=LandingVariant.conversions + 1)
        )


def _sig(secret: str, raw: bytes) -> bytes:
    return hmac.new(secret.encode(), b"confirm:" + raw, hashlib.sha256).digest()[:16]


def confirm_token(secret: str, business_id: uuid.UUID, contact_id: uuid.UUID, page_id: uuid.UUID) -> str:
    raw = business_id.bytes + contact_id.bytes + page_id.bytes
    return base64.urlsafe_b64encode(raw + _sig(secret, raw)).decode().rstrip("=")


def parse_confirm_token(secret: str, token: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID] | None:
    try:
        data = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (ValueError, TypeError):
        return None
    if len(data) != 64 or not hmac.compare_digest(_sig(secret, data[:48]), data[48:]):
        return None
    return uuid.UUID(bytes=data[:16]), uuid.UUID(bytes=data[16:32]), uuid.UUID(bytes=data[32:48])


def page_view(page: LandingPage, variants: list[LandingVariant], public_url: str) -> dict[str, Any]:
    return {
        "id": str(page.id),
        "slug": page.slug,
        "title": page.title,
        "kind": page.kind,
        "status": page.status,
        "settings": page.settings,
        "campaign_id": str(page.campaign_id) if page.campaign_id else None,
        "public_url": public_url,
        "views": page.views,
        "submissions": page.submissions,
        "conversion_rate": round(page.submissions / page.views, 4) if page.views else None,
        "published_at": page.published_at.isoformat() if page.published_at else None,
        "variants": [
            {
                "id": str(v.id),
                "key": v.key,
                "weight": v.weight,
                "content": v.content,
                "views": v.views,
                "conversions": v.conversions,
                "conversion_rate": round(v.conversions / v.views, 4) if v.views else None,
            }
            for v in variants
        ],
    }
