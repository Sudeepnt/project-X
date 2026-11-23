from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, SmallInteger, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TimestampMixin

JOB_STATUSES = ("queued", "running", "succeeded", "failed", "dead", "cancelled")


class Job(IdMixin, TimestampMixin, Base):
    """Durable background work. Enqueued inside the caller's transaction (transactional outbox),
    claimed with ``FOR UPDATE SKIP LOCKED``, retried with backoff, dead-lettered after
    ``max_attempts``. A crashed worker's lease expires and another worker picks the job up."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_ready", "priority", "run_at", postgresql_where=text("status = 'queued'")),
        Index("ix_jobs_lease", "locked_until", postgresql_where=text("status = 'running'")),
        Index(
            "uq_jobs_idempotency", "idempotency_key", unique=True, postgresql_where=text("idempotency_key IS NOT NULL")
        ),
        Index("ix_jobs_business_kind", "business_id", "kind", "created_at"),
    )

    business_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    status: Mapped[str] = mapped_column(String(20), default="queued", server_default="queued")
    priority: Mapped[int] = mapped_column(SmallInteger, default=100, server_default="100")
    run_at: Mapped[datetime] = mapped_column(server_default=func.now())
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, default=8, server_default="8")
    locked_by: Mapped[str | None] = mapped_column(String(100))
    locked_until: Mapped[datetime | None]
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    last_error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None]
    finished_at: Mapped[datetime | None]
