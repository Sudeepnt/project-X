"""Registers the Finance department with the spine."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from projectx.modules.finance import books, service
from projectx.modules.finance.api import router
from projectx.modules.finance.payments import public_router
from projectx.spine.modules import Module, register_module
from projectx.spine.tenancy.models import Business


async def _created(session: AsyncSession, business: Business) -> None:
    await books.setup(session, business)


async def _numbers(session: AsyncSession, business_id: Any) -> dict[str, Any]:
    return await service.numbers(session, business_id)


MODULE = register_module(
    Module(
        key="finance",
        title="Finance",
        description="Billing and collections, double-entry accounting, and tax.",
        routers=[router],
        public_routers=[public_router],
        on_business_created=_created,
        daily_cycle=service.daily,
        on_manual_task_done=service.tax_filed,
        numbers=_numbers,
    )
)
