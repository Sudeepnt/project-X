"""Registers the GTM module with the spine: routes, defaults, daily and weekly cycles, cockpit."""

from __future__ import annotations

import importlib
import uuid
from typing import Any

from projectx.spine.modules import Module, register_module

# Importing these registers ledger events, action types, executors and job handlers.
for _name in (
    "projectx.modules.gtm.events",
    "projectx.modules.gtm.actions",
    "projectx.modules.gtm.crm.automations",
    "projectx.modules.gtm.nurture.sender",
    "projectx.modules.gtm.nurture.newsletter",
    "projectx.modules.gtm.brand.service",
    "projectx.modules.gtm.goals.service",
    "projectx.modules.gtm.campaigns.service",
    "projectx.modules.gtm.content.pipeline",
    "projectx.modules.gtm.content.importer",
    "projectx.modules.gtm.content.repurpose",
    "projectx.modules.gtm.channels.publisher",
    "projectx.modules.gtm.channels.accounts",
    "projectx.modules.gtm.analytics.report",
    "projectx.modules.gtm.inbox.service",
    "projectx.modules.gtm.ads.service",
    "projectx.modules.gtm.analytics.benchmarks",
):
    importlib.import_module(_name)

from projectx.modules.gtm import cycles  # noqa: E402
from projectx.modules.gtm.ads.api import router as ads_router  # noqa: E402
from projectx.modules.gtm.analytics.api import public_router as analytics_public_router  # noqa: E402
from projectx.modules.gtm.analytics.api import router as analytics_router  # noqa: E402
from projectx.modules.gtm.analytics.benchmarks import router as benchmarks_router  # noqa: E402
from projectx.modules.gtm.assets import router as assets_router  # noqa: E402
from projectx.modules.gtm.community.discord import public_router as discord_public_router  # noqa: E402
from projectx.modules.gtm.community.discord import router as discord_router  # noqa: E402
from projectx.modules.gtm.compliance.api import public_router as compliance_public_router  # noqa: E402
from projectx.modules.gtm.compliance.api import router as compliance_router  # noqa: E402
from projectx.modules.gtm.crm.api import router as crm_router  # noqa: E402
from projectx.modules.gtm.crm.webhooks import public_router as payments_public_router  # noqa: E402
from projectx.modules.gtm.crm.webhooks import router as payments_router  # noqa: E402
from projectx.modules.gtm.defaults import seed_defaults  # noqa: E402
from projectx.modules.gtm.growth.api import router as growth_router  # noqa: E402
from projectx.modules.gtm.growth.public import router as growth_public_router  # noqa: E402
from projectx.modules.gtm.inbox.api import public_router as inbox_public_router  # noqa: E402
from projectx.modules.gtm.inbox.api import router as inbox_router  # noqa: E402
from projectx.modules.gtm.nurture.api import router as nurture_router  # noqa: E402
from projectx.modules.gtm.publishing_api import public_router as publishing_public_router  # noqa: E402
from projectx.modules.gtm.publishing_api import router as publishing_router  # noqa: E402
from projectx.modules.gtm.strategy_api import router as strategy_router  # noqa: E402


async def on_manual_task_done(services: Any, business_id: uuid.UUID, decision: Any) -> None:
    """A person posted a draft by hand: record it as a post so analytics and learning see it."""
    from projectx.modules.gtm.channels.publisher import record_manual_post

    if decision.action_type == "gtm.publish_manual" and decision.payload.get("variant_id"):
        url = (decision.result or {}).get("url")
        await record_manual_post(services, business_id, uuid.UUID(decision.payload["variant_id"]), url)


MODULE = register_module(
    Module(
        key="gtm",
        title="Go-to-market",
        description="Brand, goals, campaigns, content, publishing, CRM, nurture, analytics and learning.",
        routers=[
            crm_router,
            growth_router,
            payments_router,
            strategy_router,
            publishing_router,
            nurture_router,
            analytics_router,
            inbox_router,
            compliance_router,
            ads_router,
            discord_router,
            assets_router,
            benchmarks_router,
        ],
        public_routers=[
            growth_public_router,
            payments_public_router,
            publishing_public_router,
            analytics_public_router,
            inbox_public_router,
            compliance_public_router,
            discord_public_router,
        ],
        on_business_created=seed_defaults,
        daily_cycle=cycles.daily_cycle,
        weekly_cycle=cycles.weekly_cycle,
        on_manual_task_done=on_manual_task_done,
        briefing=cycles.briefing,
        numbers=cycles.numbers,
        usage_counts=cycles.usage_counts,
    )
)
