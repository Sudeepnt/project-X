"""GTM's reactions to the other departments (through the bus)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from projectx.kernel.time import utcnow
from projectx.spine.bus import BusContext, on
from projectx.spine.jobs.queue import enqueue


@on("product.release.published", "gtm.announce_release")
async def announce_release(ctx: BusContext) -> dict[str, Any] | None:
    """A release went out: draft an announcement for the connected channels. It goes through review and approval."""
    from projectx.modules.gtm.models import ChannelAccount, ContentItem
    from projectx.modules.gtm.platforms import format_for
    from projectx.modules.product.models import Release

    async with ctx.services.db.tenant(ctx.business_id) as session:
        release = await session.get(Release, uuid.UUID(ctx.data["release_id"]))
        platforms = sorted(
            set(
                (
                    await session.execute(
                        select(ChannelAccount.platform).where(ChannelAccount.status.in_(("connected", "practice")))
                    )
                ).scalars()
            )
        )
        formats = {p: f for p in platforms if (f := format_for(p, "text")) is not None}
        if release is None or not formats:
            return {"skipped": "no release or no connected channel"}
        title = f"What's new: {release.version}"
        item = ContentItem(
            business_id=ctx.business_id,
            intent="value",
            pillar="educate",
            title=title,
            core_idea=release.notes[:2000],
            source_type="release",
            status="idea",
            planned_for=utcnow() + timedelta(hours=3),
            created_by="agent:gtm",
            source_ref={
                "formats": formats,
                "platforms": list(formats),
                "mix_kind": "text",
                "theme_name": "Product news",
                "title": title,
                "summary": release.notes[:500],
            },
            ai_generated=True,
        )
        session.add(item)
        await session.flush()
        await enqueue(
            session,
            "gtm.generate_content",
            {"content_id": str(item.id)},
            business_id=ctx.business_id,
            idempotency_key=f"gen:{item.id}",
        )
        return {"content_id": str(item.id), "platforms": list(formats)}
