"""How a department module plugs into the spine.

A module is one object that tells the spine about its API routes, the defaults it seeds for a
new business, its part of the daily cycle, and what it contributes to the founder's cockpit.
Action types, ledger events and job handlers register themselves when the module is imported.
GTM is the first module; finance, legal, people and the rest plug in the same way.
"""

from __future__ import annotations

import importlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import APIRouter
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from projectx.services import Services
    from projectx.spine.decisions.models import Decision
    from projectx.spine.tenancy.models import Business

log = structlog.get_logger("modules")

MODULE_PACKAGES = (
    "projectx.modules.gtm",
    "projectx.modules.finance",
    "projectx.modules.ops",
    "projectx.modules.people",
    "projectx.modules.legal",
)


@dataclass
class BriefingItem:
    kind: str  # e.g. "goal_pace", "published", "alert"
    title: str
    body: str = ""
    level: str = "info"  # info | success | warning | critical
    link: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Module:
    key: str
    title: str
    description: str = ""
    routers: list[APIRouter] = field(default_factory=list)  # under /api/v1/b/{business_id}
    public_routers: list[APIRouter] = field(default_factory=list)  # links, pages, webhooks
    on_business_created: Callable[[AsyncSession, Business], Awaitable[None]] | None = None
    daily_cycle: Callable[[Services, uuid.UUID], Awaitable[None]] | None = None
    weekly_cycle: Callable[[Services, uuid.UUID], Awaitable[None]] | None = None
    on_manual_task_done: Callable[[Services, uuid.UUID, Decision], Awaitable[None]] | None = None
    briefing: Callable[[AsyncSession, uuid.UUID], Awaitable[list[BriefingItem]]] | None = None
    numbers: Callable[[AsyncSession, uuid.UUID], Awaitable[dict[str, Any]]] | None = None
    # Live counts of things a plan limits ("contacts", "platforms", …), for quotas and the usage page.
    usage_counts: Callable[[AsyncSession, uuid.UUID], Awaitable[dict[str, int]]] | None = None


_MODULES: dict[str, Module] = {}
_loaded = False


def register_module(module: Module) -> Module:
    _MODULES[module.key] = module
    return module


def load_modules() -> list[Module]:
    """Import every module package once. Importing registers actions, events, jobs and routes."""
    global _loaded
    if not _loaded:
        import projectx.spine.assistant.service
        import projectx.spine.calendar.service
        import projectx.spine.calendar.sync
        import projectx.spine.context.proactive
        import projectx.spine.context.store
        import projectx.spine.decisions.jobs
        import projectx.spine.events
        import projectx.spine.lifecycle  # noqa: F401

        for package in MODULE_PACKAGES:
            importlib.import_module(f"{package}.module")
        _loaded = True
    return list(_MODULES.values())


def modules() -> list[Module]:
    return list(_MODULES.values())


async def run_manual_task_hooks(services: Services, business_id: uuid.UUID, decision_id: uuid.UUID) -> None:
    from projectx.spine.decisions.models import Decision

    async with services.db.tenant(business_id) as session:
        decision = await session.get(Decision, decision_id)
    if decision is None:
        return
    for module in modules():
        if module.on_manual_task_done is not None:
            await module.on_manual_task_done(services, business_id, decision)
