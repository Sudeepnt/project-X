"""Registers the Operations department with the spine."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from projectx.modules.ops import service
from projectx.modules.ops.api import router
from projectx.spine.modules import Module, register_module


async def _numbers(session: AsyncSession, business_id: Any) -> dict[str, Any]:
    return await service.numbers(session, business_id)


MODULE = register_module(
    Module(
        key="ops",
        title="Operations",
        description="Customer support and success, delivery projects and operations.",
        routers=[router],
        numbers=_numbers,
    )
)
