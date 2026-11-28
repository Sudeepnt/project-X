"""Registers the Legal and admin department with the spine."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from projectx.modules.legal import service
from projectx.modules.legal.api import router
from projectx.spine.modules import Module, register_module


async def _numbers(session: AsyncSession, business_id: Any) -> dict[str, Any]:
    return await service.numbers(session, business_id)


MODULE = register_module(
    Module(
        key="legal",
        title="Legal and admin",
        description="Contracts and obligations; accounts, software, domains and devices; access and security.",
        routers=[router],
        daily_cycle=service.daily,
        on_manual_task_done=service.on_task_done,
        numbers=_numbers,
    )
)
