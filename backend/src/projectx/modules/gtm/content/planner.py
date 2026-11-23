"""The content planner: turns a running campaign into dated slots.

For each day it works out how many core ideas are needed (from posting frequency per platform),
which platforms each idea goes to, the content type (from the campaign's content mix), whether it
is a value post or an ask (keeping the value-to-ask ratio), the theme and the hook (chosen with
Thompson sampling over what has worked for this business), and the posting time.
"""

from __future__ import annotations

import hashlib
import math
import random
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.time import utcnow, zone
from projectx.modules.gtm.config import gtm_settings
from projectx.modules.gtm.content.calendar import upcoming
from projectx.modules.gtm.content.hooks import DEFAULT_HOOKS
from projectx.modules.gtm.learning.bandit import gaussian_pick
from projectx.modules.gtm.models import Campaign, ContentItem, ContentTheme, PatternStat
from projectx.modules.gtm.platforms import format_for
from projectx.spine.tenancy.models import Business

ASK_PILLARS = ("offer", "proof")


MIN_LEAD = timedelta(hours=1)


@dataclass
class Slot:
    when: datetime
    platforms: list[str]
    mix_kind: str
    intent: str
    theme_id: uuid.UUID | None
    theme_name: str
    pillar: str
    hook_type: str
    campaign_id: uuid.UUID
    moment: dict[str, Any] | None = None
    formats: dict[str, str] = field(default_factory=dict)


def daily_quota(per_week: float, day_index: int) -> int:
    """Spread a weekly frequency evenly: day i gets floor((i+1)*f/7) - floor(i*f/7)."""
    f = max(per_week, 0.0)
    return math.floor((day_index + 1) * f / 7) - math.floor(day_index * f / 7)


def mix_sequence(content_mix: dict[str, float], count: int) -> list[str]:
    """Largest-remainder allocation of content types across `count` ideas, interleaved."""
    if not content_mix or count <= 0:
        return ["text"] * max(count, 0)
    raw = {k: v * count for k, v in content_mix.items()}
    alloc = {k: math.floor(v) for k, v in raw.items()}
    for k in sorted(raw, key=lambda k: raw[k] - alloc[k], reverse=True)[: count - sum(alloc.values())]:
        alloc[k] += 1
    sequence: list[str] = []
    while len(sequence) < count:
        for k in sorted(alloc, key=lambda k: -alloc[k]):
            if alloc[k] > 0:
                sequence.append(k)
                alloc[k] -= 1
    return sequence[:count]


async def pattern_means(session: AsyncSession, business_id: uuid.UUID, dimension: str) -> dict[str, tuple[int, float]]:
    rows = (
        await session.execute(
            select(PatternStat.key, PatternStat.n, PatternStat.ewma).where(
                PatternStat.business_id == business_id, PatternStat.dimension == dimension
            )
        )
    ).all()
    return {key: (int(n), float(ewma)) for key, n, ewma in rows}


async def recent_ask_share(session: AsyncSession, business_id: uuid.UUID) -> tuple[int, int]:
    since = datetime.now(UTC) - timedelta(days=14)
    rows = dict(
        (
            await session.execute(
                select(ContentItem.intent, func.count())
                .where(
                    ContentItem.business_id == business_id,
                    ContentItem.created_at >= since,
                    ContentItem.status.notin_(("rejected", "archived")),
                )
                .group_by(ContentItem.intent)
            )
        )
        .tuples()
        .all()
    )
    return int(rows.get("value", 0)), int(rows.get("ask", 0))


async def plan_slots(
    session: AsyncSession,
    business: Business,
    campaign: Campaign,
    *,
    start_day: date,
    days: int,
    seed: int | None = None,
) -> list[Slot]:
    stable = int(hashlib.sha256(f"{campaign.id}:{start_day.isoformat()}".encode()).hexdigest()[:12], 16)
    rng = random.Random(seed if seed is not None else stable)  # noqa: S311 - planning variety, not security
    themes = list(
        (
            await session.execute(
                select(ContentTheme).where(ContentTheme.business_id == business.id, ContentTheme.active.is_(True))
            )
        ).scalars()
    )
    theme_stats = await pattern_means(session, business.id, "theme")
    hook_stats = await pattern_means(session, business.id, "hook_type")
    hour_stats = await pattern_means(session, business.id, "hour")
    hours = list(gtm_settings(business)["posting_hours"].get("default", [9, 12, 18]))
    if hour_stats:
        ranked = sorted(hour_stats, key=lambda h: -hour_stats[h][1])
        hours = sorted({int(h) for h in ranked[:3]} | set(hours[:1]))
    tz = zone(business.timezone)
    moments = upcoming([business.country or "US"], start_day, days=days + 2)
    value_count, ask_count = await recent_ask_share(session, business.id)
    ratio = max(float(campaign.value_ratio or 3), 0.5)
    local_day = func.date(func.timezone(business.timezone, ContentItem.planned_for))
    existing_days = set(
        (
            await session.execute(
                select(local_day).where(
                    ContentItem.business_id == business.id,
                    ContentItem.campaign_id == campaign.id,
                    ContentItem.planned_for >= datetime.combine(start_day, datetime.min.time(), tz),
                )
            )
        ).scalars()
    )

    slots: list[Slot] = []
    # The founder needs time to review: never plan a slot that starts within the next hour.
    earliest = utcnow() + MIN_LEAD
    frequency = {
        p: float(f) for p, f in (campaign.frequency or {}).items() if p in campaign.channels or not campaign.channels
    }
    for i in range(days):
        day = start_day + timedelta(days=i)
        if day in existing_days:
            continue
        quotas = {p: daily_quota(f, (day.toordinal()) % 7) for p, f in frequency.items()}
        ideas = max(quotas.values(), default=0)
        kinds = mix_sequence(campaign.content_mix or {"text": 1.0}, ideas)
        moment = next((m for m in moments if m["date"] == day.isoformat()), None)
        for j in range(ideas):
            platforms = [p for p, q in quotas.items() if q > j]
            kind = kinds[j] if j < len(kinds) else "text"
            formats = {p: f for p in platforms if (f := format_for(p, kind)) is not None}
            if not formats:
                continue
            # Keep the value-to-ask ratio: ask when asks are under 1/(ratio+1) of recent ideas.
            total = value_count + ask_count
            intent = "ask" if total and ask_count / total < 1 / (ratio + 1) and j == ideas - 1 else "value"
            if total == 0 and j == 0:
                intent = "value"
            pool = [t for t in themes if (t.pillar in ASK_PILLARS) == (intent == "ask")] or themes
            if pool:
                weighted = {
                    str(t.id): (
                        theme_stats.get(str(t.id), (0, 0.5))[0],
                        theme_stats.get(str(t.id), (0, 0.5))[1] * float(t.weight or 1.0),
                    )
                    for t in pool
                }
                chosen_id = gaussian_pick(list(weighted), weighted, rng)
                theme = next(t for t in pool if str(t.id) == chosen_id)
                theme_id, theme_name, pillar = theme.id, theme.name, theme.pillar
            else:
                theme_id, theme_name, pillar = None, "General", "educate" if intent == "value" else "offer"
            hook = gaussian_pick(list(DEFAULT_HOOKS), hook_stats, rng)
            hour = hours[j % len(hours)]
            when = datetime(day.year, day.month, day.day, int(hour), 0, tzinfo=tz).astimezone(UTC)
            if when < earliest:
                continue
            slots.append(
                Slot(
                    when=when,
                    platforms=list(formats),
                    mix_kind=kind,
                    intent=intent,
                    theme_id=theme_id,
                    theme_name=theme_name,
                    pillar=pillar,
                    hook_type=hook,
                    campaign_id=campaign.id,
                    moment=moment,
                    formats=formats,
                )
            )
            if intent == "ask":
                ask_count += 1
            else:
                value_count += 1
    return slots
