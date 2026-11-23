from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ForeignKey,
    Index,
    LargeBinary,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base
from projectx.kernel.ids import uuid7


class LedgerEvent(Base):
    """The permanent, ordered record of everything that happened in a business.

    Append-only: a database trigger rejects UPDATE, DELETE and TRUNCATE. Each event carries the
    SHA-256 of its predecessor, so any tampering breaks the chain and is detectable. Events hold
    ids, never personal data (see docs/PLAN.md rule 6).
    """

    __tablename__ = "ledger_events"
    __table_args__ = (
        UniqueConstraint("business_id", "seq"),
        Index("ix_ledger_events_business_type_time", "business_id", "type", "occurred_at"),
        Index("ix_ledger_events_business_subject", "business_id", "subject_type", "subject_id"),
        Index(
            "uq_ledger_events_idempotency",
            "business_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7, server_default=text("uuidv7()"))
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="RESTRICT"))
    seq: Mapped[int]
    occurred_at: Mapped[datetime]
    recorded_at: Mapped[datetime] = mapped_column(server_default=func.now())
    type: Mapped[str] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(SmallInteger, default=1)
    actor_type: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[str | None] = mapped_column(String(100))
    subject_type: Mapped[str | None] = mapped_column(String(50))
    subject_id: Mapped[uuid.UUID | None]
    data: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    correlation_id: Mapped[uuid.UUID | None]
    causation_id: Mapped[uuid.UUID | None]
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    prev_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    hash: Mapped[bytes] = mapped_column(LargeBinary)


class LedgerHead(Base):
    """One row per business: the latest sequence number and hash. Appends lock this row."""

    __tablename__ = "ledger_heads"

    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="RESTRICT"), primary_key=True)
    last_seq: Mapped[int] = mapped_column(default=0)
    last_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
