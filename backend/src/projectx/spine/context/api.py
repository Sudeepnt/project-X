"""See and control what the assistant knows, and when it may reach you on its own."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from projectx.api.deps import BusinessDep, EditorDep
from projectx.kernel.errors import NotFound, ValidationFailed
from projectx.kernel.time import utcnow
from projectx.spine.calendar.timing import WEEKDAYS
from projectx.spine.context import proactive, store
from projectx.spine.context.models import AgentWakeup, ContextEntry
from projectx.spine.ledger.service import Actor

router = APIRouter(prefix="/api/v1/b/{business_id}", tags=["assistant"])

Kind = Literal["preference", "person", "fact", "routine", "commitment"]


class EntryIn(BaseModel):
    kind: Kind
    key: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=500)
    for_whole_business: bool = False
    pinned: bool = False


class PinIn(BaseModel):
    pinned: bool


def _visible(ctx: BusinessDep) -> Any:
    return or_(ContextEntry.user_id.is_(None), ContextEntry.user_id == ctx.user.id)


@router.get("/context")
async def list_entries(ctx: BusinessDep, kind: str | None = None, q: str | None = None) -> dict[str, Any]:
    """Everything the assistant currently believes about you and the business."""
    stmt = select(ContextEntry).where(ContextEntry.status == "active", _visible(ctx))
    if kind:
        stmt = stmt.where(ContextEntry.kind.in_(kind.split(",")))
    if q:
        stmt = stmt.where(or_(ContextEntry.value.ilike(f"%{q}%"), ContextEntry.key.ilike(f"%{q}%")))
    rows = (await ctx.session.execute(stmt.order_by(ContextEntry.kind, ContextEntry.key).limit(500))).scalars()
    return {"items": [store.entry_view(e) for e in rows]}


@router.get("/context/{entry_id}/history")
async def history(entry_id: uuid.UUID, ctx: BusinessDep) -> dict[str, Any]:
    entry = await ctx.session.get(ContextEntry, entry_id)
    if entry is None or (entry.user_id and entry.user_id != ctx.user.id):
        raise NotFound("Nothing stored under that id")
    chain = [entry]
    while chain[-1].supersedes_id and len(chain) < 100:
        older = await ctx.session.get(ContextEntry, chain[-1].supersedes_id)
        if older is None:
            break
        chain.append(older)
    return {"items": [store.entry_view(e) for e in chain]}


@router.post("/context", status_code=201)
async def add_entry(body: EntryIn, ctx: EditorDep) -> dict[str, Any]:
    """Tell it something directly, or correct what it learned (same key replaces, history kept)."""
    if body.for_whole_business:
        ctx.require("admin")
    entry = await store.save(
        ctx.session,
        ctx.business_id,
        user_id=None if body.for_whole_business else ctx.user.id,
        kind=body.kind,
        key=body.key,
        value=body.value,
        source="person",
        confidence=1.0,
        pinned=body.pinned,
        actor=Actor.user(ctx.user.id),
    )
    return store.entry_view(entry)


async def _own(ctx: BusinessDep, entry_id: uuid.UUID) -> ContextEntry:
    entry = await ctx.session.get(ContextEntry, entry_id)
    if entry is None or entry.status != "active":
        raise NotFound("Nothing stored under that id")
    if entry.user_id is None:
        ctx.require("admin")
    elif entry.user_id != ctx.user.id:
        raise NotFound("Nothing stored under that id")
    return entry


@router.post("/context/{entry_id}/pin")
async def pin(entry_id: uuid.UUID, body: PinIn, ctx: EditorDep) -> dict[str, Any]:
    entry = await _own(ctx, entry_id)
    entry.pinned = body.pinned
    return store.entry_view(entry)


@router.delete("/context/{entry_id}", status_code=204)
async def forget(entry_id: uuid.UUID, ctx: EditorDep) -> None:
    entry = await _own(ctx, entry_id)
    await store.forget(ctx.session, ctx.business_id, entry.id, actor=Actor.user(ctx.user.id))


# ---------------------------------------------------------------- staying on


class SettingsIn(BaseModel):
    enabled: bool | None = None
    brief_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    brief_days: list[str] | None = None
    quiet_start: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    quiet_end: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    channels: list[str] | None = None
    max_nudges_per_day: int | None = Field(default=None, ge=0, le=30)


@router.get("/assistant/settings")
async def get_settings(ctx: BusinessDep) -> dict[str, Any]:
    return proactive.settings_view(await proactive.get_settings(ctx.session, ctx.business_id, ctx.user.id))


@router.put("/assistant/settings")
async def put_settings(body: SettingsIn, ctx: BusinessDep) -> dict[str, Any]:
    s = await proactive.get_settings(ctx.session, ctx.business_id, ctx.user.id)
    changes = body.model_dump(exclude_unset=True)
    if "brief_days" in changes and not set(changes["brief_days"]) <= set(WEEKDAYS):
        raise ValidationFailed(f"Days are {', '.join(WEEKDAYS)}")
    if "channels" in changes and not set(changes["channels"]) <= {"app", "email", "whatsapp"}:
        raise ValidationFailed("Channels are app, email and whatsapp")
    for k, v in changes.items():
        setattr(s, k, v)
    # Times changed: drop wakeups not yet due so the new plan takes over.
    for w in (
        await ctx.session.execute(
            select(AgentWakeup).where(
                AgentWakeup.user_id == ctx.user.id, AgentWakeup.status == "pending", AgentWakeup.kind == "brief"
            )
        )
    ).scalars():
        await ctx.session.delete(w)
    await ctx.session.flush()
    await proactive.plan_wakeups(ctx.session, ctx.business, ctx.user.id, utcnow())
    return proactive.settings_view(s)


@router.get("/assistant/wakeups")
async def upcoming(ctx: BusinessDep) -> dict[str, Any]:
    """When it plans to look at your day next, and why."""
    rows = (
        await ctx.session.execute(
            select(AgentWakeup).where(AgentWakeup.user_id == ctx.user.id).order_by(AgentWakeup.wake_at.desc()).limit(50)
        )
    ).scalars()
    return {"items": [proactive.wakeup_view(w) for w in rows]}


@router.post("/assistant/brief")
async def brief_now(ctx: EditorDep) -> dict[str, Any]:
    """Get the brief now instead of waiting for the morning."""
    return await proactive.brief_now(ctx.services, ctx.business_id, ctx.user.id)
