from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Computed, Index, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TenantMixin

SHELVES = ("case", "fact", "playbook")


class MemoryCase(IdMixin, TenantMixin, Base):
    """Memory on three shelves (blueprint §7): cases (what was tried and what happened), facts
    (what is true about this business) and playbooks (how things are done here). Agents pull the
    most similar entries before a task; learning costs almost nothing and needs no retraining."""

    __tablename__ = "memory_cases"
    __table_args__ = (
        Index("ix_memory_cases_search", "search", postgresql_using="gin"),
        Index("ix_memory_cases_scope", "business_id", "shelf", "kind", "created_at"),
    )

    shelf: Mapped[str] = mapped_column(String(10))
    kind: Mapped[str] = mapped_column(String(50))
    action_type: Mapped[str | None] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(300))
    lesson: Mapped[str] = mapped_column(Text, default="")
    situation: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    outcome: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(50)), default=list, server_default="{}")
    weight: Mapped[float] = mapped_column(default=1.0)
    search: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', coalesce(title, '') || ' ' || coalesce(lesson, ''))", persisted=True),
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
