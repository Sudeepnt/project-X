"""How departments work together without a person passing notes: subscriptions to the ledger.

Every change in the business is already a ledger event (append-only, hash-chained, no personal data).
A department subscribes to the event types it cares about; when one is appended, a delivery job is
queued for each subscriber in the same transaction, so a handoff exists exactly when the change does.

    @on("gtm.deal.changed", "finance.invoice_won_deal", when=lambda d: d["status"] == "won")
    async def invoice_it(ctx: BusContext) -> dict | None: ...

- Exactly once per (event, subscriber): the job's idempotency key, and handlers write idempotently.
- Traceable: handlers append their own events with ``ctx.cause`` so every chain (deal won → invoice
  → payment → revenue booked) can be followed back to where it started.
- Bounded: a chain longer than ``MAX_CHAIN`` stops, so two departments can never loop forever.
- Visible: every delivery is a row in ``department_handoffs`` (who handed what to whom, and the outcome).

Anything outward, costly or irreversible a handler wants to do still goes through ``propose``, so the
policy engine decides whether it runs alone or waits for a person.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Index, String, Text, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TenantMixin
from projectx.kernel.time import utcnow

if TYPE_CHECKING:
    from projectx.services import Services
    from projectx.spine.ledger.models import LedgerEvent

MAX_CHAIN = 40


class Handoff(IdMixin, TenantMixin, Base):
    __tablename__ = "department_handoffs"
    __table_args__ = (
        Index("uq_handoffs_delivery", "business_id", "event_id", "subscriber", unique=True),
        Index("ix_handoffs_recent", "business_id", "created_at"),
    )

    event_id: Mapped[uuid.UUID]  # the ledger event; no foreign key, the ledger stays untouchable
    event_type: Mapped[str] = mapped_column(String(100))
    subscriber: Mapped[str] = mapped_column(String(100))
    from_module: Mapped[str] = mapped_column(String(30))
    to_module: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(12), default="queued")  # queued | done | skipped | failed
    result: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    done_at: Mapped[datetime | None]


@dataclass
class BusContext:
    services: Services
    business_id: uuid.UUID
    event_id: uuid.UUID
    event_type: str
    data: dict[str, Any]
    subject: tuple[str, uuid.UUID] | None
    correlation_id: uuid.UUID

    @property
    def cause(self) -> dict[str, Any]:
        """Pass to ``append(..., **ctx.cause)`` so the new event links back to this one."""
        return {"correlation_id": self.correlation_id, "causation_id": self.event_id}


Handler = Callable[[BusContext], Awaitable[dict[str, Any] | None]]


@dataclass(frozen=True)
class Subscriber:
    name: str
    event_type: str
    module: str
    handler: Handler
    when: Callable[[dict[str, Any]], bool] | None = field(default=None, compare=False)


_SUBS: dict[str, list[Subscriber]] = {}


def on(
    event_type: str, name: str, *, when: Callable[[dict[str, Any]], bool] | None = None
) -> Callable[[Handler], Handler]:
    """Register a department's reaction to an event type. `name` is "<module>.<what it does>"."""

    def decorator(fn: Handler) -> Handler:
        module = name.split(".", 1)[0]
        subs = _SUBS.setdefault(event_type, [])
        if any(s.name == name for s in subs):
            raise ValueError(f"subscriber {name!r} registered twice for {event_type}")
        subs.append(Subscriber(name, event_type, module, fn, when))
        return fn

    return decorator


def subscribers(event_type: str | None = None) -> list[Subscriber]:
    if event_type is not None:
        return list(_SUBS.get(event_type, []))
    return [s for subs in _SUBS.values() for s in subs]


async def dispatch(session: AsyncSession, event: LedgerEvent) -> None:
    """Called by the ledger right after an event is appended, inside the same transaction."""
    subs = _SUBS.get(event.type)
    if not subs:
        return
    from projectx.spine.jobs.queue import enqueue

    correlation = event.correlation_id or event.id
    if event.correlation_id is not None:
        from projectx.spine.ledger.models import LedgerEvent as Ev

        depth = await session.scalar(
            select(func.count())
            .select_from(Ev)
            .where(Ev.business_id == event.business_id, Ev.correlation_id == correlation)
        )
        if (depth or 0) > MAX_CHAIN:
            return
    for sub in subs:
        if sub.when is not None and not sub.when(event.data):
            continue
        session.add(
            Handoff(
                business_id=event.business_id,
                event_id=event.id,
                event_type=event.type,
                subscriber=sub.name,
                from_module=event.type.split(".", 1)[0],
                to_module=sub.module,
            )
        )
        await enqueue(
            session,
            "spine.bus.deliver",
            {"event_id": str(event.id), "subscriber": sub.name, "correlation_id": str(correlation)},
            business_id=event.business_id,
            idempotency_key=f"bus:{event.id}:{sub.name}",
        )
    await session.flush()


def _register_job() -> None:
    from projectx.spine.jobs.registry import JobContext, job

    @job("spine.bus.deliver", max_attempts=6, timeout_s=300, priority=40)
    async def deliver(ctx: JobContext) -> dict[str, Any] | None:
        from projectx.spine.ledger.models import LedgerEvent as Ev

        business_id = ctx.require_business()
        name = ctx.payload["subscriber"]
        async with ctx.services.db.tenant(business_id) as session:
            event = await session.get(Ev, uuid.UUID(ctx.payload["event_id"]))
            if event is None:
                return {"skipped": "event gone"}
            sub = next((s for s in _SUBS.get(event.type, []) if s.name == name), None)
            bus_ctx = BusContext(
                services=ctx.services,
                business_id=business_id,
                event_id=event.id,
                event_type=event.type,
                data=dict(event.data),
                subject=(event.subject_type, event.subject_id) if event.subject_type and event.subject_id else None,
                correlation_id=uuid.UUID(ctx.payload["correlation_id"]),
            )
        if sub is None:
            await _finish(ctx.services, business_id, bus_ctx.event_id, name, "skipped", {"reason": "no subscriber"})
            return {"skipped": "no subscriber"}
        try:
            result = await sub.handler(bus_ctx) or {}
        except Exception as exc:
            if ctx.attempt >= 6:
                await _finish(ctx.services, business_id, bus_ctx.event_id, name, "failed", {}, str(exc)[:500])
            raise
        status = "skipped" if result.get("skipped") else "done"
        await _finish(ctx.services, business_id, bus_ctx.event_id, name, status, result)
        return result

    del deliver


async def _finish(
    services: Services,
    business_id: uuid.UUID,
    event_id: uuid.UUID,
    subscriber: str,
    status: str,
    result: dict[str, Any],
    error: str | None = None,
) -> None:
    async with services.db.tenant(business_id) as session:
        row = await session.scalar(
            select(Handoff).where(Handoff.event_id == event_id, Handoff.subscriber == subscriber).with_for_update()
        )
        if row is not None:
            row.status, row.result, row.error, row.done_at = status, _jsonable(result), error, utcnow()


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, uuid.UUID | datetime):
        return str(value)
    return value


def handoff_view(h: Handoff) -> dict[str, Any]:
    return {
        "id": str(h.id),
        "event_id": str(h.event_id),
        "event_type": h.event_type,
        "subscriber": h.subscriber,
        "from": h.from_module,
        "to": h.to_module,
        "status": h.status,
        "result": h.result,
        "error": h.error,
        "created_at": h.created_at.isoformat() if h.created_at else None,
        "done_at": h.done_at.isoformat() if h.done_at else None,
    }


_register_job()
