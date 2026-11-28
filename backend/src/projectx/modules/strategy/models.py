"""Strategy tables: objectives measured from department numbers, and weekly steering reviews."""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import Float, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class Objective(IdMixin, TenantMixin, TimestampMixin, Base):
    """A goal measured by a number a department already reports, e.g. "finance.revenue_mtd_minor" ≥ 500000."""

    __tablename__ = "strategy_objectives"

    title: Mapped[str] = mapped_column(String(200))
    metric: Mapped[str] = mapped_column(String(80))  # "<module>.<number key>"
    target: Mapped[float] = mapped_column(Float)
    direction: Mapped[str] = mapped_column(String(4), default="up")  # up: at least the target | down: at most
    due_date: Mapped[date]
    start_value: Mapped[float | None] = mapped_column(Float)
    last_value: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(10), default="active")  # active | met | missed | dropped


class Review(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "strategy_reviews"
    __table_args__ = (Index("uq_strategy_review_week", "business_id", "week", unique=True),)

    week: Mapped[str] = mapped_column(String(8))  # 2026-W48
    numbers: Mapped[dict[str, Any]] = mapped_column(default=dict)
    risks: Mapped[list[Any]] = mapped_column(default=list)
    objectives: Mapped[list[Any]] = mapped_column(default=list)
    summary: Mapped[str] = mapped_column(String(2000), default="")
