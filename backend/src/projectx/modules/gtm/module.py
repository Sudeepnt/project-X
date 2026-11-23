"""Registers the GTM module with the spine: routes, defaults, daily and weekly cycles, cockpit."""

from __future__ import annotations

import importlib

from projectx.spine.modules import Module, register_module

# Importing these registers ledger events, action types, executors and job handlers.
for _name in (
    "projectx.modules.gtm.events",
    "projectx.modules.gtm.actions",
    "projectx.modules.gtm.crm.automations",
    "projectx.modules.gtm.nurture.sender",
):
    importlib.import_module(_name)

from projectx.modules.gtm.crm.api import router as crm_router  # noqa: E402
from projectx.modules.gtm.crm.webhooks import public_router as payments_public_router  # noqa: E402
from projectx.modules.gtm.crm.webhooks import router as payments_router  # noqa: E402
from projectx.modules.gtm.defaults import seed_defaults  # noqa: E402
from projectx.modules.gtm.growth.api import router as growth_router  # noqa: E402
from projectx.modules.gtm.growth.public import router as growth_public_router  # noqa: E402

MODULE = register_module(
    Module(
        key="gtm",
        title="Go-to-market",
        description="Brand, goals, campaigns, content, publishing, CRM, nurture, analytics and learning.",
        routers=[crm_router, growth_router, payments_router],
        public_routers=[growth_public_router, payments_public_router],
        on_business_created=seed_defaults,
    )
)
