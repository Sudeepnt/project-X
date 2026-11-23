"""Enqueue jobs inside the caller's transaction: the job exists only if the change commits."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.ids import uuid7
from projectx.spine.jobs.models import Job
from projectx.spine.jobs.registry import get_job


async def enqueue(
    session: AsyncSession,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    business_id: uuid.UUID | None = None,
    run_at: datetime | None = None,
    priority: int | None = None,
    idempotency_key: str | None = None,
    max_attempts: int | None = None,
) -> uuid.UUID | None:
    """Returns the new job id, or None when a job with the same idempotency key already exists."""
    spec = get_job(kind)
    if spec is None:
        raise KeyError(f"no handler registered for job kind {kind!r}")
    values: dict[str, Any] = {
        "id": uuid7(),
        "kind": kind,
        "payload": payload or {},
        "business_id": business_id,
        "priority": priority if priority is not None else spec.priority,
        "max_attempts": max_attempts if max_attempts is not None else spec.max_attempts,
        "idempotency_key": idempotency_key,
    }
    if run_at is not None:
        values["run_at"] = run_at
    stmt = insert(Job).values(**values)
    if idempotency_key is not None:
        stmt = stmt.on_conflict_do_nothing(
            index_elements=[Job.idempotency_key], index_where=text("idempotency_key IS NOT NULL")
        )
    return await session.scalar(stmt.returning(Job.id))


async def cancel(session: AsyncSession, job_id: uuid.UUID) -> bool:
    result = await session.execute(
        update(Job).where(Job.id == job_id, Job.status == "queued").values(status="cancelled", finished_at=func.now())
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def pending_count(session: AsyncSession, business_id: uuid.UUID, kind: str | None = None) -> int:
    stmt = (
        select(func.count())
        .select_from(Job)
        .where(Job.business_id == business_id, Job.status.in_(("queued", "running")))
    )
    if kind:
        stmt = stmt.where(Job.kind == kind)
    return int(await session.scalar(stmt) or 0)
