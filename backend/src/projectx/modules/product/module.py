"""Registers the Product department with the spine."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from projectx.modules.product import service
from projectx.modules.product.api import router
from projectx.spine.modules import Module, register_module


async def _numbers(session: AsyncSession, business_id: Any) -> dict[str, Any]:
    return await service.numbers(session, business_id)


MODULE = register_module(
    Module(
        key="product",
        title="Product",
        description="A roadmap ranked by what customers ask for, specs, and releases.",
        routers=[router],
        numbers=_numbers,
    )
)
