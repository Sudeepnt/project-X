from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TenantMixin


class ModelCall(IdMixin, TenantMixin, Base):
    """One model call: the spending report and, once approved, training data (blueprint §8)."""

    __tablename__ = "model_calls"
    __table_args__ = (
        Index("ix_model_calls_business_time", "business_id", "created_at"),
        Index("ix_model_calls_business_task", "business_id", "task", "created_at"),
    )

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    task: Mapped[str] = mapped_column(String(100))
    rung: Mapped[str] = mapped_column(String(20))
    provider: Mapped[str] = mapped_column(String(30))
    model: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_micro_usd: Mapped[int] = mapped_column(default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cached: Mapped[bool] = mapped_column(default=False)
    request_hash: Mapped[str] = mapped_column(String(64))
    prompt: Mapped[dict[str, Any] | None]
    response: Mapped[dict[str, Any] | None]
    error: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[uuid.UUID | None]


class ModelCache(Base):
    """Never pay twice for the same answer (blueprint cost rule 2)."""

    __tablename__ = "model_cache"

    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    response: Mapped[dict[str, Any]]
    model: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]
