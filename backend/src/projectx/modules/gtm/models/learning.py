from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class Experiment(IdMixin, TimestampMixin, TenantMixin, Base):
    """An A/B test. Allocation uses Thompson sampling, so traffic shifts to winners as evidence
    builds instead of waiting for a fixed end date."""

    __tablename__ = "gtm_experiments"

    key: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(30))  # landing_page | hook | subject_line | creative | posting_time
    hypothesis: Mapped[str] = mapped_column(Text, default="")
    subject_type: Mapped[str | None] = mapped_column(String(40))
    subject_id: Mapped[uuid.UUID | None]
    metric: Mapped[str] = mapped_column(String(40), default="conversion")
    status: Mapped[str] = mapped_column(String(20), default="running")
    allocation: Mapped[str] = mapped_column(String(20), default="thompson")
    min_samples: Mapped[int] = mapped_column(Integer, default=100)
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    ended_at: Mapped[datetime | None]
    winner_key: Mapped[str | None] = mapped_column(String(20))
    result: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")


class ExperimentVariant(IdMixin, TenantMixin, Base):
    __tablename__ = "gtm_experiment_variants"
    __table_args__ = (Index("uq_gtm_experiment_variant", "experiment_id", "key", unique=True),)

    experiment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("gtm_experiments.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(20))
    label: Mapped[str] = mapped_column(String(200))
    config: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    conversions: Mapped[int] = mapped_column(Integer, default=0)


class PatternStat(TenantMixin, Base):
    """Memory of winning patterns per business: how each hook, theme, format, hour and weekday
    performs. The planner reads it, so every new campaign starts smarter."""

    __tablename__ = "gtm_pattern_stats"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    dimension: Mapped[str] = mapped_column(String(30))
    key: Mapped[str] = mapped_column(String(80))
    n: Mapped[int] = mapped_column(Integer, default=0)
    sum_score: Mapped[float] = mapped_column(Float, default=0.0)
    sum_sq: Mapped[float] = mapped_column(Float, default=0.0)
    ewma: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (Index("uq_gtm_pattern", "business_id", "dimension", "key", unique=True),)


class Report(IdMixin, TenantMixin, Base):
    """A weekly review: numbers computed by code, then explained in words by a model."""

    __tablename__ = "gtm_reports"
    __table_args__ = (Index("uq_gtm_report_period", "business_id", "kind", "period_start", unique=True),)

    kind: Mapped[str] = mapped_column(String(20), default="weekly")
    period_start: Mapped[date]
    period_end: Mapped[date]
    data: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    narrative: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
