"""Tracked links and click attribution.

Every post carries a short tracked link. A click records telemetry (no IP stored), then
redirects with UTM parameters plus a click id (``pxc``). Landing-page forms send the click id
back, which ties the new contact to the exact post, platform and campaign without needing
third-party cookies.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.errors import ValidationFailed
from projectx.kernel.ids import short_code, token_urlsafe
from projectx.modules.gtm.crm.lifecycle import Attribution
from projectx.modules.gtm.models import Campaign, LinkClick, TrackedLink
from projectx.spine.routing.models import RoutingKey
from projectx.spine.tenancy.models import Business

_BOT_UA = re.compile(
    r"bot|crawl|spider|slurp|facebookexternalhit|preview|monitor|curl|wget|python-requests|headless", re.IGNORECASE
)
_MEDIUM = {
    "newsletter": "email",
    "email": "email",
    "whatsapp": "messaging",
    "telegram": "messaging",
    "discord": "community",
    "blog": "owned",
}


def short_url(base_url: str, slug: str) -> str:
    return f"{base_url.rstrip('/')}/l/{slug}"


def _validate_destination(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValidationFailed("Links must be full http(s) URLs", extra={"field": "destination"})
    return url.strip()


async def create_link(
    session: AsyncSession,
    business: Business,
    *,
    destination: str,
    platform: str | None = None,
    campaign_id: uuid.UUID | None = None,
    content_id: uuid.UUID | None = None,
    variant_id: uuid.UUID | None = None,
    post_id: uuid.UUID | None = None,
    label: str | None = None,
    utm: dict[str, str] | None = None,
) -> TrackedLink:
    destination = _validate_destination(destination)
    campaign_slug = None
    if campaign_id:
        campaign = await session.get(Campaign, campaign_id)
        campaign_slug = campaign.playbook_key if campaign else None
    params = {
        "utm_source": platform or "direct",
        "utm_medium": _MEDIUM.get(platform or "", "social"),
        "utm_campaign": campaign_slug or "always_on",
    }
    if content_id:
        params["utm_content"] = str(content_id)[:8]
    params.update(utm or {})
    for _ in range(5):
        slug = short_code(8)
        exists = await session.scalar(
            select(RoutingKey.key).where(RoutingKey.namespace == "link", RoutingKey.key == slug)
        )
        if exists is None:
            break
    link = TrackedLink(
        business_id=business.id,
        slug=slug,
        destination=destination,
        utm=params,
        label=label,
        campaign_id=campaign_id,
        content_id=content_id,
        variant_id=variant_id,
        post_id=post_id,
        platform=platform,
    )
    session.add(link)
    await session.flush()
    await session.execute(
        insert(RoutingKey)
        .values(namespace="link", key=slug, business_id=business.id, target_id=link.id)
        .on_conflict_do_nothing()
    )
    return link


def destination_with_params(link: TrackedLink, click_id: str) -> str:
    parts = urlsplit(link.destination)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key, value in (link.utm or {}).items():
        query.setdefault(key, value)
    query["pxc"] = click_id
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def device_of(user_agent: str) -> str:
    ua = user_agent.lower()
    if "ipad" in ua or "tablet" in ua:
        return "tablet"
    if "mobi" in ua or "iphone" in ua or "android" in ua:
        return "mobile"
    return "desktop"


def visitor_hash(secret: str, ip: str | None, user_agent: str) -> str:
    """Salted per day so visitors can't be tracked across days, and no IP is ever stored."""
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    return hmac.new(f"{secret}:{day}".encode(), f"{ip}|{user_agent}".encode(), hashlib.sha256).hexdigest()[:32]


async def record_click(
    session: AsyncSession,
    link: TrackedLink,
    *,
    user_agent: str,
    referrer: str | None,
    country: str | None,
    ip: str | None,
    secret: str,
) -> str:
    click_id = token_urlsafe(12)[:16]
    referrer_host = urlsplit(referrer).netloc[:200] if referrer else None
    session.add(
        LinkClick(
            business_id=link.business_id,
            link_id=link.id,
            click_id=click_id,
            referrer_host=referrer_host,
            device=device_of(user_agent),
            country=(country or "")[:2].upper() or None,
            is_bot=bool(_BOT_UA.search(user_agent or "")),
            visitor_hash=visitor_hash(secret, ip, user_agent),
        )
    )
    await session.execute(update(TrackedLink).where(TrackedLink.id == link.id).values(clicks=TrackedLink.clicks + 1))
    return click_id


async def attribution_for_click(
    session: AsyncSession, business_id: uuid.UUID, click_id: str | None
) -> tuple[Attribution | None, LinkClick | None]:
    if not click_id:
        return None, None
    row = (
        await session.execute(
            select(LinkClick, TrackedLink)
            .join(TrackedLink, TrackedLink.id == LinkClick.link_id)
            .where(LinkClick.business_id == business_id, LinkClick.click_id == click_id[:32])
        )
    ).first()
    if row is None:
        return None, None
    click, link = row
    return Attribution(
        campaign_id=link.campaign_id,
        platform=link.platform,
        post_id=link.post_id,
        link_id=link.id,
        content_id=link.content_id,
        utm=link.utm or {},
    ), click


def link_view(link: TrackedLink, base_url: str) -> dict[str, Any]:
    return {
        "id": str(link.id),
        "slug": link.slug,
        "short_url": short_url(base_url, link.slug),
        "destination": link.destination,
        "utm": link.utm,
        "label": link.label,
        "platform": link.platform,
        "campaign_id": str(link.campaign_id) if link.campaign_id else None,
        "content_id": str(link.content_id) if link.content_id else None,
        "post_id": str(link.post_id) if link.post_id else None,
        "clicks": link.clicks,
        "created_at": link.created_at.isoformat(),
    }
