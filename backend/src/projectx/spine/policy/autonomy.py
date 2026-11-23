"""Autonomy is earned slowly and lost instantly (blueprint §10).

Up: an action type moves one level after a clean streak (50 by default: no edits, no
rejections, nothing reversed), and only when the founder says yes.
Down: one level at once after an incident, when conditions change, or when its pack ships a
new version. One-way doors never rise above Ask first, whatever the record.
"""

from __future__ import annotations

import uuid
from typing import Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.time import utcnow
from projectx.spine.ledger.service import Actor, append
from projectx.spine.policy.actions import ActionType, get_action
from projectx.spine.policy.levels import Level
from projectx.spine.policy.models import AutonomyState

Outcome = Literal["clean", "edited", "rejected", "reversed", "failed"]


async def get_state(session: AsyncSession, business_id: uuid.UUID, action: ActionType) -> AutonomyState:
    """Fetch (creating if missing) and lock the row. The insert is atomic, so concurrent jobs never collide."""
    await session.execute(
        insert(AutonomyState)
        .values(
            business_id=business_id,
            action_type=action.key,
            level=int(action.default_level),
            clean_streak=0,
            total_runs=0,
            total_edits=0,
            total_rejections=0,
            total_reversals=0,
            promotion_pending=False,
        )
        .on_conflict_do_nothing(index_elements=[AutonomyState.business_id, AutonomyState.action_type])
    )
    state = (
        await session.execute(
            select(AutonomyState)
            .where(AutonomyState.business_id == business_id, AutonomyState.action_type == action.key)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return state


async def record_outcome(
    session: AsyncSession, business_id: uuid.UUID, action_type: str, outcome: Outcome, *, threshold: int
) -> bool:
    """Update the streak. Returns True when the action type has just earned a promotion offer."""
    action = get_action(action_type)
    if action is None:
        return False
    state = await get_state(session, business_id, action)
    if outcome in ("clean", "edited"):
        state.total_runs += 1
    if outcome == "clean":
        state.clean_streak += 1
    else:
        state.clean_streak = 0
        if outcome == "edited":
            state.total_edits += 1
        elif outcome == "rejected":
            state.total_rejections += 1
        elif outcome == "reversed":
            state.total_reversals += 1
    eligible = (
        action.promotable
        and not state.promotion_pending
        and state.clean_streak >= threshold
        and state.level < int(action.max_level)
        and state.level >= int(Level.DRAFT)
    )
    if eligible:
        state.promotion_pending = True
    await session.flush()
    return eligible


async def change_level(
    session: AsyncSession, business_id: uuid.UUID, action_type: str, *, to: Level, reason: str, actor: Actor
) -> Level:
    action = get_action(action_type)
    if action is None:
        raise ValueError(f"unknown action {action_type!r}")
    state = await get_state(session, business_id, action)
    target = Level(min(int(to), int(action.max_level)))
    previous = Level(state.level)
    if target == previous:
        state.promotion_pending = False
        return previous
    state.level = int(target)
    state.clean_streak = 0
    state.promotion_pending = False
    if target > previous:
        state.last_promoted_at = utcnow()
    else:
        state.last_demoted_at = utcnow()
    await append(
        session,
        business_id=business_id,
        type="spine.autonomy.changed",
        actor=actor,
        data={"action_type": action_type, "from_level": previous.slug, "to_level": target.slug, "reason": reason},
    )
    return target


async def promote(session: AsyncSession, business_id: uuid.UUID, action_type: str, *, user_id: uuid.UUID) -> Level:
    action = get_action(action_type)
    if action is None:
        raise ValueError(f"unknown action {action_type!r}")
    state = await get_state(session, business_id, action)
    return await change_level(
        session,
        business_id,
        action_type,
        to=Level(min(state.level + 1, int(action.max_level))),
        reason="clean streak, approved by founder",
        actor=Actor.user(user_id),
    )


async def demote(
    session: AsyncSession,
    business_id: uuid.UUID,
    action_type: str,
    *,
    reason: str,
    floor: Level = Level.DRAFT,
    incident: bool = False,
) -> Level:
    action = get_action(action_type)
    if action is None:
        raise ValueError(f"unknown action {action_type!r}")
    state = await get_state(session, business_id, action)
    if incident:
        state.last_incident_at = utcnow()
    target = Level(max(state.level - 1, int(floor)))
    if target >= Level(state.level):
        return Level(state.level)
    return await change_level(
        session, business_id, action_type, to=target, reason=reason, actor=Actor.system("autonomy")
    )


async def demote_pack(session: AsyncSession, business_id: uuid.UUID, pack: str, *, version: str) -> list[str]:
    """A new pack version ships: its action types drop one level (never below Ask first)."""
    from projectx.spine.policy.actions import all_actions

    changed: list[str] = []
    for action in all_actions():
        if action.pack != pack:
            continue
        state = await get_state(session, business_id, action)
        if state.pack_version == version:
            continue
        if state.pack_version is not None:
            await demote(
                session, business_id, action.key, reason=f"pack {pack} updated to {version}", floor=Level.ASK_FIRST
            )
            changed.append(action.key)
        state.pack_version = version
    return changed
