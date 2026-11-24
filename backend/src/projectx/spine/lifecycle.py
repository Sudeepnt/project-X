"""The rhythm of a business on the platform.

Agents wake on events, not timers (blueprint cost rule 3). The one exception is the daily cycle:
once a day per business, in its own morning, each module checks pace, plans the next day and
prepares the founder's briefing (cost rule 4). The weekly cycle runs the review and the
learning updates. Both are enqueued with idempotency keys, so they run once per period even
if several schedulers ever overlapped.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from projectx.kernel.time import zone
from projectx.spine.jobs.queue import enqueue
from projectx.spine.jobs.registry import JobContext, job, periodic
from projectx.spine.modules import modules
from projectx.spine.tenancy.models import Business

if TYPE_CHECKING:
    from projectx.services import Services

log = structlog.get_logger("lifecycle")


@periodic("spine.schedule_cycles", every=timedelta(minutes=15))
async def schedule_cycles(services: Services, now: datetime) -> None:
    hour = services.settings.daily_cycle_hour_local
    async with services.db.session() as session:
        rows = (await session.execute(select(Business.id, Business.timezone).where(Business.status == "active"))).all()
        for business_id, tz in rows:
            local = now.astimezone(zone(tz))
            if local.hour < hour:
                continue
            day = local.date().isoformat()
            await enqueue(
                session,
                "spine.daily_cycle",
                {"day": day},
                business_id=business_id,
                idempotency_key=f"daily:{business_id}:{day}",
            )
            if local.weekday() == 0:  # Monday morning review
                week = f"{local.isocalendar().year}-W{local.isocalendar().week:02d}"
                await enqueue(
                    session,
                    "spine.weekly_cycle",
                    {"week": week},
                    business_id=business_id,
                    idempotency_key=f"weekly:{business_id}:{week}",
                )


@job("spine.daily_cycle", max_attempts=4, timeout_s=1800, priority=60)
async def daily_cycle(ctx: JobContext) -> dict[str, Any] | None:
    business_id = ctx.require_business()
    ran: list[str] = []
    for module in modules():
        if module.daily_cycle is None:
            continue
        try:
            await module.daily_cycle(ctx.services, business_id)
            ran.append(module.key)
        except Exception:
            ctx.log.exception("daily_cycle.module_failed", module=module.key)
            raise
    return {"modules": ran, "day": ctx.payload.get("day")}


@job("spine.weekly_cycle", max_attempts=4, timeout_s=3600, priority=70)
async def weekly_cycle(ctx: JobContext) -> dict[str, Any] | None:
    business_id = ctx.require_business()
    ran: list[str] = []
    for module in modules():
        if module.weekly_cycle is not None:
            await module.weekly_cycle(ctx.services, business_id)
            ran.append(module.key)
    return {"modules": ran, "week": ctx.payload.get("week")}


@job("spine.send_email", max_attempts=8, timeout_s=60, priority=10)
async def send_email(ctx: JobContext) -> dict[str, Any] | None:
    from sqlalchemy import text

    p = ctx.payload
    message_id = await ctx.services.mailer.send(
        to=p["to"],
        subject=p["subject"],
        html=p["html"],
        text=p["text"],
        reply_to=p.get("reply_to"),
        headers=p.get("headers"),
    )
    # The payload held an address and possibly a one-time link: drop it once delivered.
    async with ctx.services.db.session() as session:
        await session.execute(text("UPDATE jobs SET payload = '{}'::jsonb WHERE id = :id"), {"id": ctx.job_id})
    return {"message_id": message_id}


async def queue_email(
    session: Any,
    *,
    to: str,
    subject: str,
    html: str,
    text: str,
    business_id: uuid.UUID | None = None,
    reply_to: str | None = None,
    headers: dict[str, str] | None = None,
) -> None:
    await enqueue(
        session,
        "spine.send_email",
        {"to": to, "subject": subject, "html": html, "text": text, "reply_to": reply_to, "headers": headers or {}},
        business_id=business_id,
    )


@periodic("spine.prune", every=timedelta(hours=6))
async def prune(services: Services, now: datetime) -> None:
    """Housekeeping so tables don't grow forever: old throttle windows, spent or expired tokens and sessions,
    and finished jobs. The ledger and anything a customer can see are never pruned."""
    from sqlalchemy import text

    statements = (
        "DELETE FROM auth_throttle WHERE window_start < now() - interval '2 days'",
        "DELETE FROM email_tokens WHERE expires_at < now() - interval '7 days' OR used_at < now() - interval '7 days'",
        "DELETE FROM user_sessions WHERE expires_at < now() - interval '1 day'",
        "DELETE FROM jobs WHERE status = 'succeeded' AND finished_at < now() - interval '14 days'",
        "DELETE FROM jobs WHERE status = 'dead' AND finished_at < now() - interval '90 days'",
    )
    async with services.db.session() as session:
        for sql in statements:
            await session.execute(text(sql))
