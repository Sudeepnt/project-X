"""Remember and recall. Retrieval uses PostgreSQL full-text search ranked with recency, which is
free, fast and private; embeddings can be layered on later without changing callers."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.spine.memory.models import MemoryCase


async def remember(
    session: AsyncSession,
    business_id: uuid.UUID,
    *,
    shelf: str,
    kind: str,
    title: str,
    lesson: str = "",
    situation: dict[str, Any] | None = None,
    outcome: dict[str, Any] | None = None,
    tags: Iterable[str] = (),
    action_type: str | None = None,
    weight: float = 1.0,
) -> MemoryCase:
    case = MemoryCase(
        business_id=business_id,
        shelf=shelf,
        kind=kind,
        title=title[:300],
        lesson=lesson,
        situation=situation or {},
        outcome=outcome or {},
        tags=[t[:50] for t in tags],
        action_type=action_type,
        weight=weight,
    )
    session.add(case)
    await session.flush()
    return case


async def recall(
    session: AsyncSession,
    business_id: uuid.UUID,
    *,
    query: str | None = None,
    shelf: str | None = None,
    kind: str | None = None,
    tags: Iterable[str] = (),
    action_type: str | None = None,
    limit: int = 5,
) -> list[MemoryCase]:
    stmt = select(MemoryCase).where(MemoryCase.business_id == business_id)
    if shelf:
        stmt = stmt.where(MemoryCase.shelf == shelf)
    if kind:
        stmt = stmt.where(MemoryCase.kind == kind)
    if action_type:
        stmt = stmt.where(MemoryCase.action_type == action_type)
    tag_list = list(tags)
    if tag_list:
        stmt = stmt.where(MemoryCase.tags.overlap(tag_list))
    if query and query.strip():
        tsq = func.websearch_to_tsquery("english", query)
        rank = func.ts_rank(MemoryCase.search, tsq) * MemoryCase.weight
        stmt = stmt.where(MemoryCase.search.op("@@")(tsq)).order_by(rank.desc(), MemoryCase.created_at.desc())
    else:
        stmt = stmt.order_by(MemoryCase.created_at.desc())
    return list((await session.execute(stmt.limit(limit))).scalars())


def as_prompt_notes(cases: list[MemoryCase]) -> str:
    """Compact notes to place in a prompt: what was tried and what was learned."""
    if not cases:
        return ""
    lines = [f"- {c.title}: {c.lesson}".strip() for c in cases]
    return "Lessons from this business's history:\n" + "\n".join(lines)
