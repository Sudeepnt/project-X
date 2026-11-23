"""Importing content the business already makes (Bought's daily stories): RSS/Atom feeds polled
hourly, or pushed through the API. Each story becomes a content item that is adapted per platform,
so Bought's first version is mostly distribution, contact capture and learning."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

from defusedxml import ElementTree
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.time import utcnow
from projectx.modules.gtm.config import gtm_settings
from projectx.modules.gtm.models import Campaign, ContentItem
from projectx.modules.gtm.platforms import format_for
from projectx.spine.jobs.queue import enqueue
from projectx.spine.jobs.registry import JobContext, job, periodic
from projectx.spine.safe_fetch import fetch_public
from projectx.spine.tenancy.models import Business

if TYPE_CHECKING:
    from projectx.services import Services

_ATOM = "{http://www.w3.org/2005/Atom}"
_MEDIA = "{http://search.yahoo.com/mrss/}"


def _text(node: Any, tag: str) -> str:
    child = node.find(tag)
    return (child.text or "").strip() if child is not None and child.text else ""


def parse_feed(xml: str) -> list[dict[str, Any]]:
    root = ElementTree.fromstring(xml.encode() if isinstance(xml, str) else xml)
    items: list[dict[str, Any]] = []
    for node in root.iter("item"):
        image = None
        media = node.find(f"{_MEDIA}content") or node.find(f"{_MEDIA}thumbnail")
        enclosure = node.find("enclosure")
        if media is not None:
            image = media.get("url")
        elif enclosure is not None and (enclosure.get("type") or "").startswith("image"):
            image = enclosure.get("url")
        published = _text(node, "pubDate")
        items.append(
            {
                "title": _text(node, "title"),
                "summary": _text(node, "description")[:2000],
                "url": _text(node, "link"),
                "external_id": _text(node, "guid") or _text(node, "link"),
                "image_url": image,
                "published_at": _parse_date(published),
            }
        )
    for node in root.iter(f"{_ATOM}entry"):
        link = node.find(f"{_ATOM}link")
        items.append(
            {
                "title": _text(node, f"{_ATOM}title"),
                "summary": (_text(node, f"{_ATOM}summary") or _text(node, f"{_ATOM}content"))[:2000],
                "url": link.get("href") if link is not None else "",
                "external_id": _text(node, f"{_ATOM}id"),
                "image_url": None,
                "published_at": _text(node, f"{_ATOM}updated") or None,
            }
        )
    return [i for i in items if i["title"]]


def _parse_date(value: str) -> str | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        return None


async def import_items(
    session: AsyncSession,
    business: Business,
    items: list[dict[str, Any]],
    *,
    campaign_id: uuid.UUID | None = None,
    source: str = "api",
) -> list[uuid.UUID]:
    campaign = (
        await session.get(Campaign, campaign_id)
        if campaign_id
        else await session.scalar(
            select(Campaign)
            .where(Campaign.business_id == business.id, Campaign.status == "active")
            .order_by(Campaign.role, Campaign.created_at)
            .limit(1)
        )
    )
    channels = (campaign.channels if campaign else None) or ["instagram", "x", "threads", "telegram", "newsletter"]
    formats = {p: f for p in channels if (f := format_for(p, "image")) is not None}
    created: list[uuid.UUID] = []
    for raw in items[:100]:
        external = str(raw.get("external_id") or raw.get("url") or raw.get("title"))
        key = hashlib.sha256(external.encode()).hexdigest()[:32]
        exists = await session.scalar(
            select(ContentItem.id).where(
                ContentItem.business_id == business.id, ContentItem.source_ref["import_key"].astext == key
            )
        )
        if exists:
            continue
        item = ContentItem(
            business_id=business.id,
            campaign_id=campaign.id if campaign else None,
            pillar="educate",
            intent="value",
            hook_type="curiosity_gap",
            title=str(raw.get("title", ""))[:300],
            core_idea=str(raw.get("summary", ""))[:600],
            source_type="imported",
            status="idea",
            planned_for=utcnow() + timedelta(minutes=30),
            source_ref={
                "import_key": key,
                "source": source,
                "title": raw.get("title"),
                "summary": raw.get("summary"),
                "url": raw.get("url"),
                "image_url": raw.get("image_url"),
                "published_at": raw.get("published_at"),
                "formats": formats,
                "platforms": list(formats),
                "mix_kind": "image",
                "theme_name": "Daily stories",
            },
        )
        session.add(item)
        await session.flush()
        await enqueue(
            session,
            "gtm.generate_content",
            {"content_id": str(item.id)},
            business_id=business.id,
            idempotency_key=f"gen:{item.id}",
        )
        created.append(item.id)
    return created


@job("gtm.import_feeds", max_attempts=3, timeout_s=300, priority=60)
async def import_feeds(ctx: JobContext) -> dict[str, Any] | None:
    business_id = ctx.require_business()
    async with ctx.services.db.session() as session:
        business = await session.get(Business, business_id)
    if business is None:
        return None
    feeds: list[str] = list(gtm_settings(business).get("feeds", []))
    total = 0
    for url in feeds[:5]:
        try:
            result = await fetch_public(url)
            items = parse_feed(result.text)
        except Exception as exc:
            ctx.log.warning("feed.failed", url=url, error=str(exc))
            continue
        async with ctx.services.db.tenant(business_id) as session:
            biz = await session.get(Business, business_id)
            assert biz is not None
            total += len(await import_items(session, biz, items, source=f"feed:{url}"))
    return {"imported": total}


@periodic("gtm.schedule_imports", every=timedelta(hours=1))
async def schedule_imports(services: Services, now: datetime) -> None:
    window = now.strftime("%Y%m%d%H")
    async with services.db.session() as session:
        rows = (await session.execute(select(Business.id, Business.settings).where(Business.status == "active"))).all()
        for business_id, settings in rows:
            if ((settings or {}).get("gtm") or {}).get("feeds"):
                await enqueue(
                    session,
                    "gtm.import_feeds",
                    business_id=business_id,
                    idempotency_key=f"feeds:{business_id}:{window}",
                )
