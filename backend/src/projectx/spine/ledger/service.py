"""Append to and verify the ledger.

``append`` must be called inside the same transaction as the state change it records, so the
record and the change commit together or not at all ("nothing happens without a ledger entry").
Appends for one business are serialised by a row lock on its ``ledger_heads`` row, which gives
gap-free sequence numbers and a linear hash chain.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import orjson
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.ids import uuid7
from projectx.kernel.time import utcnow
from projectx.spine.ledger.events import get_event_type
from projectx.spine.ledger.models import LedgerEvent, LedgerHead

ActorType = Literal["user", "agent", "system", "integration", "professional", "public"]


@dataclass(frozen=True, slots=True)
class Actor:
    type: ActorType
    id: str | None = None

    @classmethod
    def user(cls, user_id: uuid.UUID | str) -> Actor:
        return cls("user", str(user_id))

    @classmethod
    def agent(cls, name: str) -> Actor:
        return cls("agent", name)

    @classmethod
    def system(cls, name: str = "system") -> Actor:
        return cls("system", name)

    @classmethod
    def integration(cls, name: str) -> Actor:
        return cls("integration", name)

    @classmethod
    def public(cls) -> Actor:
        return cls("public", None)

    def label(self) -> str:
        return f"{self.type}:{self.id}" if self.id else self.type


def _canonical_ts(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def compute_hash(
    *,
    business_id: uuid.UUID,
    seq: int,
    event_id: uuid.UUID,
    occurred_at: datetime,
    type: str,
    version: int,
    actor_type: str,
    actor_id: str | None,
    subject_type: str | None,
    subject_id: uuid.UUID | None,
    data: dict[str, Any],
    correlation_id: uuid.UUID | None,
    causation_id: uuid.UUID | None,
    prev_hash: bytes | None,
) -> bytes:
    material = orjson.dumps(
        {
            "b": str(business_id),
            "s": seq,
            "i": str(event_id),
            "t": _canonical_ts(occurred_at),
            "y": type,
            "v": version,
            "a": [actor_type, actor_id],
            "o": [subject_type, str(subject_id) if subject_id else None],
            "d": data,
            "c": str(correlation_id) if correlation_id else None,
            "k": str(causation_id) if causation_id else None,
            "p": prev_hash.hex() if prev_hash else None,
        },
        option=orjson.OPT_SORT_KEYS,
    )
    return hashlib.sha256(material).digest()


async def append(
    session: AsyncSession,
    *,
    business_id: uuid.UUID,
    type: str,
    actor: Actor,
    data: dict[str, Any] | None = None,
    subject: tuple[str, uuid.UUID] | None = None,
    occurred_at: datetime | None = None,
    correlation_id: uuid.UUID | None = None,
    causation_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
) -> LedgerEvent:
    event_type = get_event_type(type)
    payload = event_type.validate(data or {})

    if idempotency_key is not None:
        existing = await session.scalar(
            select(LedgerEvent).where(
                LedgerEvent.business_id == business_id, LedgerEvent.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            return existing

    await session.execute(insert(LedgerHead).values(business_id=business_id, last_seq=0).on_conflict_do_nothing())
    head = (
        await session.execute(select(LedgerHead).where(LedgerHead.business_id == business_id).with_for_update())
    ).scalar_one()

    seq = head.last_seq + 1
    event_id = uuid7()
    moment = occurred_at or utcnow()
    subject_type, subject_id = subject if subject else (None, None)
    digest = compute_hash(
        business_id=business_id,
        seq=seq,
        event_id=event_id,
        occurred_at=moment,
        type=type,
        version=event_type.version,
        actor_type=actor.type,
        actor_id=actor.id,
        subject_type=subject_type,
        subject_id=subject_id,
        data=payload,
        correlation_id=correlation_id,
        causation_id=causation_id,
        prev_hash=head.last_hash,
    )
    event = LedgerEvent(
        id=event_id,
        business_id=business_id,
        seq=seq,
        occurred_at=moment,
        type=type,
        version=event_type.version,
        actor_type=actor.type,
        actor_id=actor.id,
        subject_type=subject_type,
        subject_id=subject_id,
        data=payload,
        correlation_id=correlation_id,
        causation_id=causation_id,
        idempotency_key=idempotency_key,
        prev_hash=head.last_hash,
        hash=digest,
    )
    session.add(event)
    head.last_seq = seq
    head.last_hash = digest
    await session.flush()
    return event


@dataclass(frozen=True)
class ChainReport:
    ok: bool
    checked: int
    head_seq: int
    first_broken_seq: int | None = None
    problem: str | None = None


async def verify_chain(session: AsyncSession, business_id: uuid.UUID, *, batch: int = 2000) -> ChainReport:
    """Recompute every hash in order. Detects edits, deletions, insertions and reordering."""
    prev_hash: bytes | None = None
    expected_seq = 1
    checked = 0
    last_seq = 0
    while True:
        rows = (
            (
                await session.execute(
                    select(LedgerEvent)
                    .where(LedgerEvent.business_id == business_id, LedgerEvent.seq >= expected_seq)
                    .order_by(LedgerEvent.seq)
                    .limit(batch)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            break
        for ev in rows:
            if ev.seq != expected_seq:
                return ChainReport(False, checked, last_seq, expected_seq, "missing event (gap in sequence)")
            if ev.prev_hash != prev_hash:
                return ChainReport(False, checked, last_seq, ev.seq, "previous-hash link broken")
            recomputed = compute_hash(
                business_id=ev.business_id,
                seq=ev.seq,
                event_id=ev.id,
                occurred_at=ev.occurred_at,
                type=ev.type,
                version=ev.version,
                actor_type=ev.actor_type,
                actor_id=ev.actor_id,
                subject_type=ev.subject_type,
                subject_id=ev.subject_id,
                data=ev.data,
                correlation_id=ev.correlation_id,
                causation_id=ev.causation_id,
                prev_hash=ev.prev_hash,
            )
            if recomputed != ev.hash:
                return ChainReport(False, checked, last_seq, ev.seq, "event content does not match its hash")
            prev_hash = ev.hash
            expected_seq += 1
            checked += 1
            last_seq = ev.seq
    head = await session.get(LedgerHead, business_id)
    if head is not None and head.last_seq != last_seq:
        return ChainReport(False, checked, last_seq, last_seq + 1, "head points past the last event")
    return ChainReport(True, checked, last_seq)
