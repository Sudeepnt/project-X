"""Restrictions: time, money, volume, approval, brand and guardrail limits.

They are turned into policy-engine violations before anything is published, so a restriction
is enforced by code, not by a model remembering an instruction.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.errors import ValidationFailed
from projectx.kernel.time import start_of_day_utc, zone
from projectx.modules.gtm.models import Goal, Post, Restriction
from projectx.spine.policy.engine import Violation

KINDS: dict[str, str] = {
    "allowed_platforms": "Only publish on these platforms",
    "blackout_dates": "Never publish on these dates",
    "max_posts_per_day": "Maximum posts per day (per platform and in total)",
    "max_outreach_per_day": "Maximum outreach messages per day",
    "daily_budget": "Daily money limit for this goal",
    "total_budget": "Total money limit for this goal",
    "ai_credits": "Daily AI credit limit (cents)",
    "do_not_list": "Words and topics the brand never touches",
    "no_unproven_claims": "Every claim needs proof",
    "cpl_cap": "Cost per lead never above this",
    "unsubscribe_rate_cap": "Unsubscribe rate stays under this",
    "complaint_rate_cap": "Complaint rate stays under this",
    "negative_sentiment_spike": "Pause when negative replies spike above this share",
}


def validate(kind: str, params: dict[str, Any]) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValidationFailed(f"Unknown restriction {kind!r}")
    if kind == "allowed_platforms" and not params.get("platforms"):
        raise ValidationFailed("List at least one platform")
    if kind == "blackout_dates":
        for d in params.get("dates", []):
            date.fromisoformat(d)
    if kind == "max_posts_per_day" and not (params.get("total") or params.get("per_platform")):
        raise ValidationFailed("Set a total or a per-platform limit")
    if kind in ("unsubscribe_rate_cap", "complaint_rate_cap", "negative_sentiment_spike"):
        rate = float(params.get("rate", -1))
        if not 0 < rate < 1:
            raise ValidationFailed("Rates are between 0 and 1, e.g. 0.005 for 0.5%")
    return params


async def active(session: AsyncSession, business_id: uuid.UUID) -> list[Restriction]:
    goal_id = await session.scalar(select(Goal.id).where(Goal.business_id == business_id, Goal.status == "active"))
    stmt = select(Restriction).where(Restriction.business_id == business_id, Restriction.active.is_(True))
    rows = (await session.execute(stmt)).scalars().all()
    return [r for r in rows if r.goal_id is None or r.goal_id == goal_id]


def banned_phrases(restrictions: list[Restriction]) -> list[str]:
    out: list[str] = []
    for r in restrictions:
        if r.kind == "do_not_list":
            out.extend(str(p) for p in r.params.get("phrases", []))
    return out


def claims_strict(restrictions: list[Restriction]) -> bool:
    return any(r.kind == "no_unproven_claims" and r.params.get("enabled", True) for r in restrictions)


async def publish_violations(
    session: AsyncSession,
    business_id: uuid.UUID,
    *,
    platform: str,
    when: datetime,
    tz: str,
    restrictions: list[Restriction] | None = None,
) -> list[Violation]:
    rules = restrictions if restrictions is not None else await active(session, business_id)
    out: list[Violation] = []
    local_day = when.astimezone(zone(tz)).date()
    for r in rules:
        if r.kind == "allowed_platforms" and platform not in r.params.get("platforms", []):
            out.append(Violation("platform_not_allowed", f"{platform} is not on the allowed list"))
        elif r.kind == "blackout_dates" and local_day.isoformat() in r.params.get("dates", []):
            out.append(Violation("blackout_date", f"{local_day:%d %b} is a blackout date"))
        elif r.kind == "max_posts_per_day":
            start = start_of_day_utc(local_day, tz)
            end = start + timedelta(days=1)
            base = (
                select(func.count())
                .select_from(Post)
                .where(
                    Post.business_id == business_id,
                    Post.status.in_(("queued", "publishing", "published")),
                    func.coalesce(Post.published_at, Post.scheduled_at) >= start,
                    func.coalesce(Post.published_at, Post.scheduled_at) < end,
                )
            )
            per_platform = (r.params.get("per_platform") or {}).get(platform)
            if per_platform is not None:
                count = int(await session.scalar(base.where(Post.platform == platform)) or 0)
                if count >= int(per_platform):
                    out.append(Violation("daily_platform_cap", f"Already {count} posts on {platform} that day"))
            total = r.params.get("total")
            if total is not None:
                count = int(await session.scalar(base) or 0)
                if count >= int(total):
                    out.append(Violation("daily_total_cap", f"Already {count} posts that day"))
    return out


def restriction_view(r: Restriction) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "kind": r.kind,
        "label": KINDS.get(r.kind, r.kind),
        "params": r.params,
        "active": r.active,
        "goal_id": str(r.goal_id) if r.goal_id else None,
        "note": r.note,
    }
