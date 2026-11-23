"""Loading the founder's policy settings and the money already moved today."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.time import start_of_day_utc, today_in
from projectx.spine.ledger.models import LedgerEvent
from projectx.spine.policy.models import PolicySettings
from projectx.spine.tenancy.models import Business

APPROVAL_MODES = ("approve_all", "approve_new", "auto")


async def get_policy(session: AsyncSession, business_id: uuid.UUID) -> PolicySettings:
    row = await session.get(PolicySettings, business_id)
    if row is None:
        business = await session.get(Business, business_id)
        currency = business.currency if business else "USD"
        row = PolicySettings(
            business_id=business_id,
            currency=currency,
            per_action_limit_minor=5_000,
            per_day_limit_minor=20_000,
            approval_mode="approve_all",
            promotion_threshold=50,
            level_caps={},
            degraded_modules={},
        )
        session.add(row)
        await session.flush()
    return row


async def spent_today_minor(session: AsyncSession, business_id: uuid.UUID) -> int:
    business = await session.get(Business, business_id)
    tz = business.timezone if business else "UTC"
    start = start_of_day_utc(today_in(tz), tz)
    total = await session.scalar(
        select(func.coalesce(func.sum(text("(data->>'amount_minor')::bigint")), 0))
        .select_from(LedgerEvent)
        .where(
            LedgerEvent.business_id == business_id,
            LedgerEvent.type == "spine.action.executed",
            LedgerEvent.occurred_at >= start,
            text("data ? 'amount_minor'"),
        )
    )
    return int(total or 0)


def policy_view(row: PolicySettings) -> dict[str, Any]:
    return {
        "currency": row.currency,
        "per_action_limit_minor": row.per_action_limit_minor,
        "per_day_limit_minor": row.per_day_limit_minor,
        "approval_mode": row.approval_mode,
        "promotion_threshold": row.promotion_threshold,
        "level_caps": row.level_caps,
        "degraded_modules": row.degraded_modules,
    }
