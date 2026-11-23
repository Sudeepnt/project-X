"""Engine, sessions and the tenant context.

Every tenant-scoped transaction runs ``set_config('app.business_id', <id>, true)`` first. The
``true`` makes it transaction-local, so a pooled connection never leaks one tenant's context
into the next request. Row-level security policies read that setting; with no tenant set,
tenant tables are invisible and inserts into them fail. The database fails closed.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import orjson
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, set | frozenset):
        return sorted(value)
    raise TypeError(f"Type is not JSON serializable: {type(value).__name__}")


def json_dumps(value: Any) -> str:
    return orjson.dumps(value, default=_json_default, option=orjson.OPT_NON_STR_KEYS).decode()


async def set_tenant(session: AsyncSession, business_id: uuid.UUID) -> None:
    await session.execute(text("select set_config('app.business_id', :b, true)"), {"b": str(business_id)})


class Database:
    def __init__(self, url: str, *, pool_size: int = 10, max_overflow: int = 10, echo: bool = False) -> None:
        self.engine: AsyncEngine = create_async_engine(
            url,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_pre_ping=True,
            pool_recycle=1800,
            echo=echo,
            json_serializer=json_dumps,
            json_deserializer=orjson.loads,
        )
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A transaction without tenant context: only global tables are visible."""
        async with self.sessionmaker() as session, session.begin():
            yield session

    @asynccontextmanager
    async def tenant(self, business_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
        """A transaction scoped to one business. Commits on success, rolls back on error."""
        async with self.sessionmaker() as session, session.begin():
            await set_tenant(session, business_id)
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
