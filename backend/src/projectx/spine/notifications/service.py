from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.time import utcnow
from projectx.spine.notifications.models import Notification


async def notify(
    session: AsyncSession,
    business_id: uuid.UUID,
    *,
    kind: str,
    title: str,
    body: str = "",
    link: str | None = None,
    level: str = "info",
    user_id: uuid.UUID | None = None,
) -> Notification:
    note = Notification(
        business_id=business_id, user_id=user_id, kind=kind, level=level, title=title[:200], body=body, link=link
    )
    session.add(note)
    await session.flush()
    return note


async def feed(
    session: AsyncSession, business_id: uuid.UUID, user_id: uuid.UUID, *, unread_only: bool = False, limit: int = 50
) -> list[Notification]:
    stmt = (
        select(Notification)
        .where(
            Notification.business_id == business_id,
            or_(Notification.user_id.is_(None), Notification.user_id == user_id),
        )
        .order_by(Notification.created_at.desc())
        .limit(limit)
    )
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    return list((await session.execute(stmt)).scalars())


async def mark_read(
    session: AsyncSession, business_id: uuid.UUID, user_id: uuid.UUID, ids: list[uuid.UUID] | None = None
) -> int:
    stmt = update(Notification).where(
        Notification.business_id == business_id,
        Notification.read_at.is_(None),
        or_(Notification.user_id.is_(None), Notification.user_id == user_id),
    )
    if ids:
        stmt = stmt.where(Notification.id.in_(ids))
    result = await session.execute(stmt.values(read_at=utcnow()))
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def notification_view(n: Notification) -> dict[str, Any]:
    return {
        "id": str(n.id),
        "kind": n.kind,
        "level": n.level,
        "title": n.title,
        "body": n.body,
        "link": n.link,
        "created_at": n.created_at.isoformat(),
        "read": n.read_at is not None,
    }
