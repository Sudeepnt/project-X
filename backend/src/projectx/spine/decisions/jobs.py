"""Background jobs that execute approved decisions, undo them, and expire stale ones."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from projectx.kernel.time import utcnow
from projectx.spine.decisions.executors import ExecutionContext, get_executor, get_undoer
from projectx.spine.decisions.models import Decision
from projectx.spine.decisions.service import offer_promotion
from projectx.spine.jobs.registry import (
    JobContext,
    PermanentFailure,
    RetryLater,
    get_job,
    job,
    periodic,
)
from projectx.spine.ledger.service import Actor, append
from projectx.spine.notifications.service import notify
from projectx.spine.policy import autonomy
from projectx.spine.policy.autonomy import Outcome
from projectx.spine.policy.service import get_policy

if TYPE_CHECKING:
    from projectx.services import Services


def _subject(d: Decision) -> tuple[str, uuid.UUID] | None:
    return (d.subject_type, d.subject_id) if d.subject_type and d.subject_id else None


@job("spine.execute_decision", max_attempts=6, timeout_s=600, priority=20)
async def execute_decision(ctx: JobContext) -> dict[str, Any] | None:
    business_id = ctx.require_business()
    decision_id = uuid.UUID(ctx.payload["decision_id"])
    db = ctx.services.db

    async with db.tenant(business_id) as session:
        d = await session.scalar(select(Decision).where(Decision.id == decision_id).with_for_update())
        if d is None:
            raise PermanentFailure("decision not found")
        if d.status in ("executed", "done", "undone"):
            return {"skipped": d.status}
        if d.status not in ("approved", "executing"):
            return {"skipped": d.status}
        d.status = "executing"
        exec_ctx = ExecutionContext(
            services=ctx.services,
            business_id=business_id,
            decision_id=d.id,
            action_type=d.action_type,
            payload=dict(d.payload),
            option=d.chosen_option,
        )
        action_type, kind, edited = d.action_type, d.kind, d.edited

    executor = get_executor(action_type)
    if executor is None:
        await _mark_failed(ctx, business_id, decision_id, f"no executor registered for {action_type}")
        raise PermanentFailure(f"no executor for {action_type}")

    try:
        result = await executor(exec_ctx)
    except RetryLater:
        await _set_status(ctx, business_id, decision_id, "approved")
        raise
    except PermanentFailure as exc:
        await _mark_failed(ctx, business_id, decision_id, str(exc))
        raise
    except Exception as exc:
        spec = get_job(ctx.kind)
        if spec is not None and ctx.attempt >= spec.max_attempts:
            await _mark_failed(ctx, business_id, decision_id, str(exc))
        else:
            await _set_status(ctx, business_id, decision_id, "approved")
        raise

    async with db.tenant(business_id) as session:
        d = await session.scalar(select(Decision).where(Decision.id == decision_id).with_for_update())
        assert d is not None
        d.status, d.executed_at = "executed", utcnow()
        d.result = {**result.data, "summary": result.summary} if result.summary else result.data
        d.undo_payload = result.undo_payload
        data: dict[str, Any] = {"action_type": action_type, "decision_id": str(decision_id)}
        if result.amount_minor is not None:
            data.update(amount_minor=result.amount_minor, currency=result.currency)
        if result.external_ref:
            data["external_ref"] = result.external_ref[:200]
        await append(
            session,
            business_id=business_id,
            type="spine.action.executed",
            actor=Actor.agent(d.requested_by),
            data=data,
            subject=_subject(d),
            causation_id=decision_id,
        )
        policy = await get_policy(session, business_id)
        outcome: Outcome = "edited" if edited else "clean"
        if kind in ("auto", "approval"):
            earned = await autonomy.record_outcome(
                session, business_id, action_type, outcome, threshold=policy.promotion_threshold
            )
            if earned:
                await offer_promotion(session, business_id, action_type)
    return {"executed": str(decision_id)}


@job("spine.undo_decision", max_attempts=5, timeout_s=300, priority=10)
async def undo_decision(ctx: JobContext) -> dict[str, Any] | None:
    business_id = ctx.require_business()
    decision_id = uuid.UUID(ctx.payload["decision_id"])
    async with ctx.services.db.tenant(business_id) as session:
        d = await session.scalar(select(Decision).where(Decision.id == decision_id))
        if d is None or d.status != "executed":
            return {"skipped": True}
        exec_ctx = ExecutionContext(
            services=ctx.services,
            business_id=business_id,
            decision_id=d.id,
            action_type=d.action_type,
            payload=dict(d.payload),
            undo_payload=dict(d.undo_payload or {}),
        )
    undo_fn = get_undoer(exec_ctx.action_type)
    if undo_fn is None:
        raise PermanentFailure(f"no undoer for {exec_ctx.action_type}")
    detail = await undo_fn(exec_ctx)
    async with ctx.services.db.tenant(business_id) as session:
        d = await session.scalar(select(Decision).where(Decision.id == decision_id).with_for_update())
        assert d is not None
        d.status = "undone"
        d.result = {**(d.result or {}), "undo": detail}
        await append(
            session,
            business_id=business_id,
            type="spine.action.undone",
            actor=Actor.user(ctx.payload.get("user_id", "unknown")),
            data={"action_type": d.action_type, "decision_id": str(d.id)},
            subject=_subject(d),
        )
        policy = await get_policy(session, business_id)
        await autonomy.record_outcome(
            session, business_id, d.action_type, "reversed", threshold=policy.promotion_threshold
        )
        await notify(session, business_id, kind="action.undone", title=f"Undone: {d.title}")
    return {"undone": str(decision_id)}


@job("spine.after_manual_task", max_attempts=5)
async def after_manual_task(ctx: JobContext) -> dict[str, Any] | None:
    """Hook for modules: a person finished a draft-only task (e.g. posted to Reddit by hand)."""
    from projectx.spine.modules import run_manual_task_hooks

    business_id = ctx.require_business()
    await run_manual_task_hooks(ctx.services, business_id, uuid.UUID(ctx.payload["decision_id"]))
    return None


async def _set_status(ctx: JobContext, business_id: uuid.UUID, decision_id: uuid.UUID, status: str) -> None:
    async with ctx.services.db.tenant(business_id) as session:
        d = await session.scalar(select(Decision).where(Decision.id == decision_id).with_for_update())
        if d is not None and d.status == "executing":
            d.status = status


async def _mark_failed(ctx: JobContext, business_id: uuid.UUID, decision_id: uuid.UUID, error: str) -> None:
    async with ctx.services.db.tenant(business_id) as session:
        d = await session.scalar(select(Decision).where(Decision.id == decision_id).with_for_update())
        if d is None:
            return
        d.status = "failed"
        d.result = {"error": error[:2000]}
        await append(
            session,
            business_id=business_id,
            type="spine.action.failed",
            actor=Actor.system("executor"),
            data={"action_type": d.action_type, "decision_id": str(d.id), "error": error[:500]},
            subject=_subject(d),
        )
        policy = await get_policy(session, business_id)
        await autonomy.record_outcome(
            session, business_id, d.action_type, "failed", threshold=policy.promotion_threshold
        )
        await notify(
            session,
            business_id,
            kind="action.failed",
            level="critical",
            title=f"Could not complete: {d.title}",
            body=error[:300],
            link=f"/decisions/{d.id}",
        )


@periodic("spine.expire_decisions", every=timedelta(minutes=10))
async def expire_decisions(services: Services, now: datetime) -> None:
    from projectx.spine.tenancy.models import Business

    async with services.db.session() as session:
        business_ids = list((await session.execute(select(Business.id).where(Business.status == "active"))).scalars())
    for business_id in business_ids:
        async with services.db.tenant(business_id) as session:
            rows = (
                (
                    await session.execute(
                        select(Decision)
                        .where(
                            Decision.status == "pending", Decision.expires_at.is_not(None), Decision.expires_at < now
                        )
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for d in rows:
                d.status = "expired"
                await append(
                    session,
                    business_id=business_id,
                    type="spine.decision.expired",
                    actor=Actor.system("scheduler"),
                    data={"decision_id": str(d.id), "action_type": d.action_type, "kind": d.kind},
                )


__all__ = ["execute_decision", "expire_decisions", "undo_decision"]
