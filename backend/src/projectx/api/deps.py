"""Request dependencies.

``BusinessDep`` is the workhorse: it checks the signed-in user is a member of the business in the
URL, opens one transaction with the tenant context set (so row-level security applies), and
commits it *before* the response is sent (``scope="function"``), so a failed commit is never
reported to the client as success.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.db.database import set_tenant
from projectx.kernel.errors import Forbidden, NotFound, Unauthorized
from projectx.services import Services
from projectx.spine.identity.models import User
from projectx.spine.identity.security import csrf_valid
from projectx.spine.identity.service import SESSION_COOKIE, AuthContext, resolve_session
from projectx.spine.tenancy.models import Business
from projectx.spine.tenancy.service import get_membership, role_at_least

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def get_services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


ServicesDep = Annotated[Services, Depends(get_services)]


async def db_session(services: ServicesDep) -> AsyncIterator[AsyncSession]:
    async with services.db.session() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(db_session, scope="function")]


def client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


async def optional_auth(request: Request, services: ServicesDep) -> AuthContext | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    async with services.db.session() as session:
        return await resolve_session(session, token, services.settings)


async def require_auth(
    request: Request, services: ServicesDep, auth: Annotated[AuthContext | None, Depends(optional_auth)]
) -> AuthContext:
    if auth is None:
        raise Unauthorized()
    if request.method not in _SAFE_METHODS and not csrf_valid(
        auth.token_hash, request.headers.get("x-csrf-token"), services.settings.secret_key.get_secret_value()
    ):
        raise Forbidden("Missing or invalid CSRF token. Reload the page and try again.", code="csrf_failed")
    return auth


AuthDep = Annotated[AuthContext, Depends(require_auth)]
OptionalAuthDep = Annotated[AuthContext | None, Depends(optional_auth)]


@dataclass
class BusinessContext:
    session: AsyncSession
    business: Business
    user: User
    role: str
    services: Services

    @property
    def business_id(self) -> uuid.UUID:
        return self.business.id

    def require(self, minimum: str) -> None:
        if not role_at_least(self.role, minimum):
            raise Forbidden(f"This needs the {minimum} role or higher.")


async def business_context(
    business_id: uuid.UUID, auth: AuthDep, services: ServicesDep
) -> AsyncIterator[BusinessContext]:
    async with services.db.sessionmaker() as session, session.begin():
        membership = await get_membership(session, auth.user.id, business_id)
        business = await session.get(Business, business_id) if membership else None
        if membership is None or business is None or business.status == "deleted":
            raise NotFound("Business not found")
        await set_tenant(session, business_id)
        yield BusinessContext(
            session=session, business=business, user=auth.user, role=membership.role, services=services
        )


BusinessDep = Annotated[BusinessContext, Depends(business_context, scope="function")]


def require_role(minimum: str) -> Callable[[BusinessContext], Coroutine[Any, Any, BusinessContext]]:
    async def checker(ctx: BusinessDep) -> BusinessContext:
        ctx.require(minimum)
        return ctx

    return checker


EditorDep = Annotated[BusinessContext, Depends(require_role("editor"))]
AdminDep = Annotated[BusinessContext, Depends(require_role("admin"))]
OwnerDep = Annotated[BusinessContext, Depends(require_role("owner"))]
