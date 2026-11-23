"""Content pipeline jobs. Planning is wired; generation, checks and publish proposals come next
(see docs/PLAN.md). Planned slots are stored as content ideas the cockpit can show today."""

from __future__ import annotations

import uuid
from typing import Any

from projectx.kernel.time import today_in
from projectx.modules.gtm.content.planner import plan_slots
from projectx.modules.gtm.models import Campaign, ContentItem
from projectx.spine.jobs.registry import JobContext, job
from projectx.spine.tenancy.models import Business


@job("gtm.plan_content", max_attempts=3, timeout_s=300, priority=50)
async def plan_content(ctx: JobContext) -> dict[str, Any] | None:
    business_id = ctx.require_business()
    days = int(ctx.payload.get("days", 7))
    async with ctx.services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        campaign = await session.get(Campaign, uuid.UUID(ctx.payload["campaign_id"]))
        if business is None or campaign is None or campaign.status != "active":
            return None
        slots = await plan_slots(session, business, campaign, start_day=today_in(business.timezone), days=days)
        for slot in slots:
            session.add(
                ContentItem(
                    business_id=business_id,
                    campaign_id=campaign.id,
                    theme_id=slot.theme_id,
                    pillar=slot.pillar,
                    intent=slot.intent,
                    hook_type=slot.hook_type,
                    title=f"{slot.theme_name}: {slot.mix_kind}",
                    core_idea="",
                    status="idea",
                    planned_for=slot.when,
                    source_ref={
                        "platforms": slot.platforms,
                        "formats": slot.formats,
                        "mix_kind": slot.mix_kind,
                        "moment": slot.moment,
                    },
                )
            )
    return {"planned": len(slots)}
