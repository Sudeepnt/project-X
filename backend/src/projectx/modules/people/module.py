"""Registers the People department with the spine."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from projectx.modules.people import service
from projectx.modules.people.api import router
from projectx.spine.modules import Module, register_module


async def _numbers(session: AsyncSession, business_id: Any) -> dict[str, Any]:
    return await service.numbers(session, business_id)


MODULE = register_module(
    Module(
        key="people",
        title="People",
        description="Hiring, leaving, leave and payroll.",
        routers=[router],
        daily_cycle=service.daily,
        numbers=_numbers,
    )
)
