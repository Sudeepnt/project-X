"""Proposing actions and handling the founder's decisions.

Every agent action goes through ``propose``: the policy engine sorts it, the outcome is written
to the ledger, and then it either runs (Run / Run and tell), waits on a decision card (Ask first),
becomes a manual task (Draft), or is blocked (Never). A decision card always says what the agent
wants to do, why now, the forecast and confidence, the cost, what happens if the answer is no,
and how to undo it (blueprint §10).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.errors import Conflict, NotFound, ValidationFailed
from projectx.kernel.time import utcnow
from projectx.spine.decisions.models import Decision
from projectx.spine.jobs.queue import enqueue
from projectx.spine.ledger.service import Actor, append
from projectx.spine.memory.service import remember
from projectx.spine.notifications.service import notify
from projectx.spine.policy import autonomy
from projectx.spine.policy.actions import get_action
from projectx.spine.policy.engine import (
    MoneyContext,
    PolicyInput,
    PolicyVerdict,
    Violation,
    evaluate,
)
from projectx.spine.policy.levels import Level
from projectx.spine.policy.service import get_policy, spent_today_minor

REJECT_REASONS = {
    "off_brand": "Off brand",
    "wrong_facts": "Wrong facts",
    "bad_timing": "Bad timing",
    "too_salesy": "Too salesy",
    "not_needed": "Not needed",
    "too_expensive": "Too expensive",
    "other": "Other",
}


@dataclass
class Proposal:
    action_type: str
    title: str
    summary: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    preview: dict[str, Any] | None = None
    amount_minor: int | None = None
    currency: str | None = None
    new_kind: bool = False
    violations: tuple[Violation, ...] = ()
    why_now: str | None = None
    forecast: dict[str, Any] | None = None
    if_no: str | None = None
    undo: str | None = None
    subject: tuple[str, uuid.UUID] | None = None
    requested_by: str = "agent"
    priority: int = 50
    expires_at: datetime | None = None
    options: list[dict[str, Any]] | None = None
    correlation_id: uuid.UUID | None = None


@dataclass(frozen=True)
class ProposalOutcome:
    verdict: PolicyVerdict
    decision_id: uuid.UUID | None
    job_id: uuid.UUID | None

    @property
    def status(self) -> str:
        if self.verdict.blocked:
            return "blocked"
        if self.verdict.runs_now:
            return "running"
        return "waiting_for_approval" if self.verdict.needs_approval else "manual_task"


async def assess(session: AsyncSession, business_id: uuid.UUID, p: Proposal) -> PolicyVerdict:
    """Run the policy engine for a proposal without recording anything."""
    action = get_action(p.action_type)
    policy = await get_policy(session, business_id)
    earned = Level.NEVER
    if action is not None:
        state = await autonomy.get_state(session, business_id, action)
        earned = Level(state.level)
    cap_slug = policy.level_caps.get(p.action_type)
    money = None
    if p.amount_minor:
        money = MoneyContext(
            amount_minor=p.amount_minor,
            per_action_limit_minor=policy.per_action_limit_minor,
            per_day_limit_minor=policy.per_day_limit_minor,
            spent_today_minor=await spent_today_minor(session, business_id),
        )
    return evaluate(
        PolicyInput(
            action=action,
            earned_level=earned,
            approval_mode=policy.approval_mode,  # type: ignore[arg-type]
            founder_cap=Level.from_slug(cap_slug) if cap_slug else None,
            money=money,
            new_kind=p.new_kind,
            degraded=bool(action and action.module in policy.degraded_modules),
            violations=p.violations,
        )
    )


async def propose(session: AsyncSession, business_id: uuid.UUID, p: Proposal) -> ProposalOutcome:
    verdict = await assess(session, business_id, p)
    reason_codes = [r.code for r in verdict.reasons]
    actor = Actor.agent(p.requested_by)

    if verdict.blocked:
        await append(
            session,
            business_id=business_id,
            type="spine.action.blocked",
            actor=actor,
            data={"action_type": p.action_type, "reasons": reason_codes},
            subject=p.subject,
            correlation_id=p.correlation_id,
        )
        return ProposalOutcome(verdict, None, None)

    # Choices always wait for a person, whatever the level.
    kind = (
        "choice"
        if p.options
        else ("auto" if verdict.runs_now else ("approval" if verdict.needs_approval else "manual_task"))
    )
    action = get_action(p.action_type)
    decision = Decision(
        business_id=business_id,
        action_type=p.action_type,
        kind=kind,
        status="approved" if kind == "auto" else "pending",
        level=int(verdict.level),
        title=p.title[:200],
        summary=p.summary,
        why_now=p.why_now,
        forecast=p.forecast,
        cost_minor=p.amount_minor,
        currency=p.currency,
        if_no=p.if_no,
        undo=p.undo or (action.undo if action else None),
        payload=p.payload,
        preview=p.preview,
        options=p.options,
        reasons=[{"code": r.code, "message": r.message, "cap": r.cap.slug} for r in verdict.reasons],
        subject_type=p.subject[0] if p.subject else None,
        subject_id=p.subject[1] if p.subject else None,
        priority=p.priority,
        expires_at=p.expires_at,
        requested_by=p.requested_by[:100],
        correlation_id=p.correlation_id,
    )
    session.add(decision)
    await session.flush()
    await append(
        session,
        business_id=business_id,
        type="spine.action.proposed",
        actor=actor,
        data={
            "action_type": p.action_type,
            "level": verdict.level.slug,
            "decision_id": str(decision.id),
            "reasons": reason_codes,
        },
        subject=p.subject,
        correlation_id=p.correlation_id,
    )

    job_id = None
    if kind == "auto":
        job_id = await enqueue(
            session,
            "spine.execute_decision",
            {"decision_id": str(decision.id)},
            business_id=business_id,
            idempotency_key=f"exec:{decision.id}",
        )
        if verdict.level == Level.RUN_AND_TELL:
            await notify(
                session, business_id, kind="action.ran", title=p.title, body=p.summary, link=f"/decisions/{decision.id}"
            )
    elif kind in ("approval", "choice"):
        await notify(
            session,
            business_id,
            kind="decision.pending",
            title=f"Needs your decision: {p.title}",
            body=p.summary,
            link=f"/decisions/{decision.id}",
            level="warning" if p.priority <= 20 else "info",
        )
    else:
        await notify(
            session,
            business_id,
            kind="task.manual",
            title=f"Ready for you to do: {p.title}",
            body=p.summary,
            link=f"/decisions/{decision.id}",
        )
    return ProposalOutcome(verdict, decision.id, job_id)


async def _load(
    session: AsyncSession, business_id: uuid.UUID, decision_id: uuid.UUID, *, lock: bool = True
) -> Decision:
    stmt = select(Decision).where(Decision.id == decision_id, Decision.business_id == business_id)
    if lock:
        stmt = stmt.with_for_update()
    decision = await session.scalar(stmt)
    if decision is None:
        raise NotFound("Decision not found")
    return decision


async def approve(
    session: AsyncSession,
    business_id: uuid.UUID,
    decision_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    payload_patch: dict[str, Any] | None = None,
    option: str | None = None,
    note: str | None = None,
    result: dict[str, Any] | None = None,
) -> Decision:
    d = await _load(session, business_id, decision_id)
    if d.status != "pending":
        raise Conflict(f"This decision is already {d.status}")
    if d.expires_at is not None and d.expires_at < utcnow():
        d.status = "expired"
        raise Conflict("This decision has expired")
    d.decided_by, d.decided_at, d.note = user_id, utcnow(), note
    actor = Actor.user(user_id)
    event = {"decision_id": str(d.id), "action_type": d.action_type, "kind": d.kind}

    if d.kind == "promotion":
        await autonomy.promote(session, business_id, d.action_type, user_id=user_id)
        d.status = "executed"
        d.executed_at = utcnow()
    elif d.kind == "manual_task":
        d.status, d.result, d.executed_at = "done", result or {}, utcnow()
        await append(
            session,
            business_id=business_id,
            type="spine.decision.completed",
            actor=actor,
            data=event,
            subject=_subject(d),
        )
        await enqueue(
            session,
            "spine.after_manual_task",
            {"decision_id": str(d.id)},
            business_id=business_id,
            idempotency_key=f"manual:{d.id}",
        )
        return d
    else:
        if d.kind == "choice":
            valid = {o.get("key") for o in (d.options or [])}
            if option not in valid:
                raise ValidationFailed("Pick one of the options")
            d.chosen_option = option
        if payload_patch:
            d.payload = {**d.payload, **payload_patch}
            d.edited = True
        d.status = "approved"
        await enqueue(
            session,
            "spine.execute_decision",
            {"decision_id": str(d.id)},
            business_id=business_id,
            idempotency_key=f"exec:{d.id}",
        )
    await append(
        session,
        business_id=business_id,
        type="spine.decision.approved",
        actor=actor,
        data={**event, "edited": d.edited, "option": d.chosen_option},
        subject=_subject(d),
    )
    return d


async def reject(
    session: AsyncSession,
    business_id: uuid.UUID,
    decision_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    reason_code: str,
    note: str | None = None,
) -> Decision:
    if reason_code not in REJECT_REASONS:
        raise ValidationFailed("Unknown reason")
    d = await _load(session, business_id, decision_id)
    if d.status != "pending":
        raise Conflict(f"This decision is already {d.status}")
    d.status, d.decided_by, d.decided_at = "rejected", user_id, utcnow()
    d.reason_code, d.note = reason_code, note
    await append(
        session,
        business_id=business_id,
        type="spine.decision.rejected",
        actor=Actor.user(user_id),
        data={"decision_id": str(d.id), "action_type": d.action_type, "kind": d.kind, "reason_code": reason_code},
        subject=_subject(d),
    )
    if d.kind == "promotion":
        state_action = get_action(d.action_type)
        if state_action is not None:
            state = await autonomy.get_state(session, business_id, state_action)
            state.promotion_pending = False
            state.clean_streak = 0
        return d
    policy = await get_policy(session, business_id)
    await autonomy.record_outcome(session, business_id, d.action_type, "rejected", threshold=policy.promotion_threshold)
    # Every rejection becomes a case in memory, so the next attempt is better (blueprint §10).
    await remember(
        session,
        business_id,
        shelf="case",
        kind="decision_rejected",
        action_type=d.action_type,
        title=f"Rejected: {d.title}",
        lesson=f"{REJECT_REASONS[reason_code]}. {note or ''}".strip(),
        situation={"decision_id": str(d.id), "payload_keys": sorted(d.payload.keys())},
        outcome={"reason_code": reason_code},
        tags=[d.action_type, reason_code],
    )
    return d


async def cancel(session: AsyncSession, business_id: uuid.UUID, decision_id: uuid.UUID, *, reason: str) -> None:
    d = await _load(session, business_id, decision_id)
    if d.status == "pending":
        d.status, d.note = "cancelled", reason


async def request_undo(
    session: AsyncSession, business_id: uuid.UUID, decision_id: uuid.UUID, user_id: uuid.UUID
) -> Decision:
    d = await _load(session, business_id, decision_id)
    if d.status != "executed" or not d.undo_payload:
        raise Conflict("This action cannot be undone")
    await enqueue(
        session,
        "spine.undo_decision",
        {"decision_id": str(d.id), "user_id": str(user_id)},
        business_id=business_id,
        idempotency_key=f"undo:{d.id}",
    )
    return d


async def offer_promotion(session: AsyncSession, business_id: uuid.UUID, action_type: str) -> uuid.UUID | None:
    action = get_action(action_type)
    if action is None:
        return None
    state = await autonomy.get_state(session, business_id, action)
    target = Level(min(state.level + 1, int(action.max_level)))
    d = Decision(
        business_id=business_id,
        action_type=action_type,
        kind="promotion",
        status="pending",
        level=state.level,
        title=f"Let '{action.title}' move up to '{target.label}'?",
        summary=f"The last {state.clean_streak} times ran with no edits and nothing reversed.",
        why_now="It has earned a clean record.",
        if_no="It keeps asking first, as now.",
        undo="You can lower it again any time in Settings → Autonomy.",
        payload={},
        reasons=[],
        requested_by="spine.autonomy",
        priority=60,
    )
    session.add(d)
    await session.flush()
    await notify(session, business_id, kind="decision.pending", title=d.title, link=f"/decisions/{d.id}")
    return d.id


def _subject(d: Decision) -> tuple[str, uuid.UUID] | None:
    return (d.subject_type, d.subject_id) if d.subject_type and d.subject_id else None
