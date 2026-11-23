"""Spine endpoints scoped to one business: the decision queue, notifications, the ledger,
policy and autonomy settings, usage and compute, and the cockpit's morning view."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from projectx.api.deps import BusinessDep, EditorDep, OwnerDep
from projectx.kernel.errors import NotFound, ValidationFailed
from projectx.kernel.pagination import clamp_limit, decode_cursor, encode_cursor
from projectx.spine.billing.plans import PLAN_ORDER, PLANS, plan_for, usage
from projectx.spine.decisions import service as decisions
from projectx.spine.decisions.models import Decision
from projectx.spine.gateway.models import ModelCall
from projectx.spine.ledger.models import LedgerEvent
from projectx.spine.ledger.service import Actor, append, verify_chain
from projectx.spine.modules import modules
from projectx.spine.notifications.models import Notification
from projectx.spine.notifications.service import feed, mark_read, notification_view
from projectx.spine.policy import autonomy
from projectx.spine.policy.actions import all_actions, get_action
from projectx.spine.policy.levels import Level
from projectx.spine.policy.service import APPROVAL_MODES, get_policy, policy_view, spent_today_minor

router = APIRouter(prefix="/api/v1/b/{business_id}", tags=["spine"])
public_router = APIRouter(prefix="/api/v1", tags=["public"])


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def decision_view(d: Decision) -> dict[str, Any]:
    return {
        "id": str(d.id),
        "action_type": d.action_type,
        "kind": d.kind,
        "status": d.status,
        "level": Level(d.level).slug,
        "level_label": Level(d.level).label,
        "title": d.title,
        "summary": d.summary,
        "why_now": d.why_now,
        "forecast": d.forecast,
        "cost_minor": d.cost_minor,
        "currency": d.currency,
        "if_no": d.if_no,
        "undo": d.undo,
        "payload": d.payload,
        "preview": d.preview,
        "options": d.options,
        "reasons": d.reasons,
        "subject_type": d.subject_type,
        "subject_id": str(d.subject_id) if d.subject_id else None,
        "priority": d.priority,
        "expires_at": _iso(d.expires_at),
        "requested_by": d.requested_by,
        "decided_by": str(d.decided_by) if d.decided_by else None,
        "decided_at": _iso(d.decided_at),
        "reason_code": d.reason_code,
        "note": d.note,
        "edited": d.edited,
        "chosen_option": d.chosen_option,
        "executed_at": _iso(d.executed_at),
        "result": d.result,
        "can_undo": d.status == "executed" and bool(d.undo_payload),
        "created_at": _iso(d.created_at),
    }


# ---------------------------------------------------------------- decisions


@router.get("/decisions")
async def list_decisions(
    ctx: BusinessDep,
    status: str | None = None,
    kind: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    n = clamp_limit(limit)
    stmt = select(Decision).where(Decision.business_id == ctx.business_id)
    if status:
        stmt = stmt.where(Decision.status.in_(status.split(",")))
    if kind:
        stmt = stmt.where(Decision.kind.in_(kind.split(",")))
    if cursor:
        ts, last_id = decode_cursor(cursor)
        stmt = stmt.where((Decision.created_at < ts) | ((Decision.created_at == ts) & (Decision.id < last_id)))
    if status == "pending":
        stmt = stmt.order_by(Decision.priority, Decision.created_at.desc(), Decision.id.desc())
    else:
        stmt = stmt.order_by(Decision.created_at.desc(), Decision.id.desc())
    rows = list((await ctx.session.execute(stmt.limit(n + 1))).scalars())
    next_cursor = (
        encode_cursor(rows[n - 1].created_at, rows[n - 1].id) if len(rows) > n and status != "pending" else None
    )
    return {"items": [decision_view(d) for d in rows[:n]], "next_cursor": next_cursor}


@router.get("/decisions/reasons")
async def reject_reasons(ctx: BusinessDep) -> list[dict[str, str]]:
    return [{"code": k, "label": v} for k, v in decisions.REJECT_REASONS.items()]


@router.get("/decisions/{decision_id}")
async def get_decision(decision_id: uuid.UUID, ctx: BusinessDep) -> dict[str, Any]:
    d = await ctx.session.get(Decision, decision_id)
    if d is None:
        raise NotFound("Decision not found")
    return decision_view(d)


class ApproveIn(BaseModel):
    payload_patch: dict[str, Any] | None = None
    option: str | None = None
    note: str | None = Field(default=None, max_length=2000)
    result: dict[str, Any] | None = None


class RejectIn(BaseModel):
    reason_code: str
    note: str | None = Field(default=None, max_length=2000)


@router.post("/decisions/{decision_id}/approve")
async def approve_decision(decision_id: uuid.UUID, body: ApproveIn, ctx: EditorDep) -> dict[str, Any]:
    d = await decisions.approve(
        ctx.session,
        ctx.business_id,
        decision_id,
        ctx.user.id,
        payload_patch=body.payload_patch,
        option=body.option,
        note=body.note,
        result=body.result,
    )
    return decision_view(d)


@router.post("/decisions/{decision_id}/reject")
async def reject_decision(decision_id: uuid.UUID, body: RejectIn, ctx: EditorDep) -> dict[str, Any]:
    d = await decisions.reject(
        ctx.session, ctx.business_id, decision_id, ctx.user.id, reason_code=body.reason_code, note=body.note
    )
    return decision_view(d)


@router.post("/decisions/{decision_id}/undo")
async def undo_decision(decision_id: uuid.UUID, ctx: EditorDep) -> dict[str, Any]:
    d = await decisions.request_undo(ctx.session, ctx.business_id, decision_id, ctx.user.id)
    return decision_view(d)


# ---------------------------------------------------------------- notifications


@router.get("/notifications")
async def notifications(ctx: BusinessDep, unread: bool = False, limit: int = 50) -> dict[str, Any]:
    items = await feed(ctx.session, ctx.business_id, ctx.user.id, unread_only=unread, limit=clamp_limit(limit))
    unread_count = await ctx.session.scalar(
        select(func.count())
        .select_from(Notification)
        .where(
            Notification.business_id == ctx.business_id,
            Notification.read_at.is_(None),
            (Notification.user_id.is_(None)) | (Notification.user_id == ctx.user.id),
        )
    )
    return {"items": [notification_view(n) for n in items], "unread": int(unread_count or 0)}


class ReadIn(BaseModel):
    ids: list[uuid.UUID] | None = None


@router.post("/notifications/read")
async def read_notifications(body: ReadIn, ctx: BusinessDep) -> dict[str, int]:
    return {"marked": await mark_read(ctx.session, ctx.business_id, ctx.user.id, body.ids)}


# ---------------------------------------------------------------- ledger


@router.get("/ledger")
async def ledger(
    ctx: BusinessDep,
    type: str | None = None,
    subject_type: str | None = None,
    subject_id: uuid.UUID | None = None,
    before_seq: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    n = clamp_limit(limit)
    stmt = select(LedgerEvent).where(LedgerEvent.business_id == ctx.business_id)
    if type:
        stmt = stmt.where(LedgerEvent.type.startswith(type.rstrip("*")))
    if subject_type:
        stmt = stmt.where(LedgerEvent.subject_type == subject_type)
    if subject_id:
        stmt = stmt.where(LedgerEvent.subject_id == subject_id)
    if before_seq:
        stmt = stmt.where(LedgerEvent.seq < before_seq)
    rows = list((await ctx.session.execute(stmt.order_by(LedgerEvent.seq.desc()).limit(n + 1))).scalars())
    items = [
        {
            "seq": e.seq,
            "type": e.type,
            "occurred_at": e.occurred_at.isoformat(),
            "actor_type": e.actor_type,
            "actor_id": e.actor_id,
            "subject_type": e.subject_type,
            "subject_id": str(e.subject_id) if e.subject_id else None,
            "data": e.data,
            "hash": e.hash.hex()[:16],
        }
        for e in rows[:n]
    ]
    return {"items": items, "next_before_seq": rows[n - 1].seq if len(rows) > n else None}


@router.get("/ledger/verify")
async def ledger_verify(ctx: BusinessDep) -> dict[str, Any]:
    report = await verify_chain(ctx.session, ctx.business_id)
    return {
        "ok": report.ok,
        "checked": report.checked,
        "head_seq": report.head_seq,
        "first_broken_seq": report.first_broken_seq,
        "problem": report.problem,
    }


# ---------------------------------------------------------------- policy and autonomy


class PolicyPatch(BaseModel):
    per_action_limit_minor: int | None = Field(default=None, ge=0)
    per_day_limit_minor: int | None = Field(default=None, ge=0)
    approval_mode: Literal["approve_all", "approve_new", "auto"] | None = None
    promotion_threshold: int | None = Field(default=None, ge=5, le=500)
    level_caps: dict[str, str] | None = None


@router.get("/policy")
async def get_policy_settings(ctx: BusinessDep) -> dict[str, Any]:
    policy = await get_policy(ctx.session, ctx.business_id)
    return {
        **policy_view(policy),
        "spent_today_minor": await spent_today_minor(ctx.session, ctx.business_id),
        "approval_modes": list(APPROVAL_MODES),
    }


@router.patch("/policy")
async def update_policy(body: PolicyPatch, ctx: OwnerDep) -> dict[str, Any]:
    policy = await get_policy(ctx.session, ctx.business_id)
    data = body.model_dump(exclude_unset=True)
    if "level_caps" in data and data["level_caps"] is not None:
        caps: dict[str, str] = {}
        for action_type, slug in data["level_caps"].items():
            if get_action(action_type) is None:
                raise ValidationFailed(f"Unknown action {action_type}")
            Level.from_slug(slug)
            caps[action_type] = slug
        data["level_caps"] = caps
    for key, value in data.items():
        setattr(policy, key, value)
    if data:
        await append(
            ctx.session,
            business_id=ctx.business_id,
            type="spine.policy.updated",
            actor=Actor.user(ctx.user.id),
            data={"fields": sorted(data)},
        )
    return policy_view(policy)


@router.get("/autonomy")
async def autonomy_overview(ctx: BusinessDep) -> list[dict[str, Any]]:
    policy = await get_policy(ctx.session, ctx.business_id)
    out = []
    for action in all_actions():
        state = await autonomy.get_state(ctx.session, ctx.business_id, action)
        out.append(
            {
                "action_type": action.key,
                "module": action.module,
                "title": action.title,
                "description": action.description,
                "level": Level(state.level).slug,
                "level_label": Level(state.level).label,
                "max_level": action.max_level.slug,
                "one_way_door": action.one_way_door,
                "moves_money": action.moves_money,
                "public": action.public,
                "clean_streak": state.clean_streak,
                "threshold": policy.promotion_threshold,
                "total_runs": state.total_runs,
                "total_edits": state.total_edits,
                "total_rejections": state.total_rejections,
                "total_reversals": state.total_reversals,
                "promotion_pending": state.promotion_pending,
                "cap": policy.level_caps.get(action.key),
            }
        )
    return out


class LevelIn(BaseModel):
    level: Literal["never", "draft", "ask_first", "run_and_tell", "run"]


@router.put("/autonomy/{action_type}")
async def set_autonomy(action_type: str, body: LevelIn, ctx: OwnerDep) -> dict[str, Any]:
    if get_action(action_type) is None:
        raise NotFound("Unknown action")
    level = await autonomy.change_level(
        ctx.session,
        ctx.business_id,
        action_type,
        to=Level.from_slug(body.level),
        reason="set by founder",
        actor=Actor.user(ctx.user.id),
    )
    return {"action_type": action_type, "level": level.slug}


# ---------------------------------------------------------------- usage and compute


@router.get("/usage")
async def usage_overview(ctx: BusinessDep) -> dict[str, Any]:
    plan = plan_for(ctx.business.plan)
    metrics = {
        metric: {"used": await usage(ctx.session, ctx.business_id, metric), "limit": limit}
        for metric, limit in plan.limits.items()
        if metric != "ai_daily_cents"
    }
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    spent_today = int(
        await ctx.session.scalar(
            select(func.coalesce(func.sum(ModelCall.cost_micro_usd), 0)).where(
                ModelCall.business_id == ctx.business_id, ModelCall.created_at >= start
            )
        )
        or 0
    )
    week = await ctx.session.execute(
        select(
            ModelCall.task,
            func.count(),
            func.sum(ModelCall.cost_micro_usd),
            func.count().filter(ModelCall.cached.is_(True)),
        )
        .where(ModelCall.business_id == ctx.business_id, ModelCall.created_at >= start - timedelta(days=7))
        .group_by(ModelCall.task)
        .order_by(func.sum(ModelCall.cost_micro_usd).desc())
        .limit(10)
    )
    by_rung = await ctx.session.execute(
        select(ModelCall.rung, func.count())
        .where(
            ModelCall.business_id == ctx.business_id,
            ModelCall.created_at >= start - timedelta(days=7),
            ModelCall.status == "ok",
        )
        .group_by(ModelCall.rung)
    )
    cap_cents = plan.limits.get("ai_daily_cents")
    return {
        "plan": {"key": plan.key, "name": plan.name, "price_usd_month": plan.price_usd_month},
        "metrics": metrics,
        "compute": {
            "spent_today_micro_usd": spent_today,
            "cap_micro_usd": int(cap_cents) * 10_000 if cap_cents is not None else None,
            "costliest_tasks": [
                {"task": t, "calls": c, "cost_micro_usd": int(s or 0), "cached": int(k or 0)}
                for t, c, s, k in week.all()
            ],
            "steps_by_rung": dict(by_rung.tuples().all()),
        },
        "models": ctx.services.gateway.describe(),
    }


# ---------------------------------------------------------------- cockpit


@router.get("/cockpit")
async def cockpit(ctx: BusinessDep) -> dict[str, Any]:
    numbers: dict[str, Any] = {}
    briefing: list[dict[str, Any]] = []
    for module in modules():
        if module.numbers is not None:
            numbers.update(await module.numbers(ctx.session, ctx.business_id))
        if module.briefing is not None:
            for item in await module.briefing(ctx.session, ctx.business_id):
                briefing.append(
                    {
                        "module": module.key,
                        "kind": item.kind,
                        "title": item.title,
                        "body": item.body,
                        "level": item.level,
                        "link": item.link,
                        "data": item.data,
                    }
                )
    pending = list(
        (
            await ctx.session.execute(
                select(Decision)
                .where(Decision.business_id == ctx.business_id, Decision.status == "pending")
                .order_by(Decision.priority, Decision.created_at.desc())
                .limit(8)
            )
        ).scalars()
    )
    pending_count = await ctx.session.scalar(
        select(func.count())
        .select_from(Decision)
        .where(Decision.business_id == ctx.business_id, Decision.status == "pending")
    )
    return {
        "business": {
            "id": str(ctx.business_id),
            "name": ctx.business.name,
            "timezone": ctx.business.timezone,
            "currency": ctx.business.currency,
            "practice_mode": ctx.business.practice_mode,
        },
        "user": {"name": ctx.user.name},
        "numbers": numbers,
        "briefing": briefing,
        "decisions": {"count": int(pending_count or 0), "items": [decision_view(d) for d in pending]},
    }


# ---------------------------------------------------------------- public: plans


@public_router.get("/plans")
async def plans() -> list[dict[str, Any]]:
    return [
        {
            "key": p.key,
            "name": p.name,
            "tagline": p.tagline,
            "price_usd_month": p.price_usd_month,
            "limits": p.limits,
            "features": sorted(p.features),
            "ad_spend_share_bps": p.ad_spend_share_bps,
            "highlights": list(p.highlights),
        }
        for p in (PLANS[k] for k in PLAN_ORDER)
    ]
