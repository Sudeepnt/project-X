from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Integer, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, TimestampMixin


class PolicySettings(TimestampMixin, Base):
    """The founder's rules for one business: money limits, approval mode, per-action caps."""

    __tablename__ = "policy_settings"

    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True)
    currency: Mapped[str] = mapped_column(String(3))
    per_action_limit_minor: Mapped[int] = mapped_column(default=5_000)
    per_day_limit_minor: Mapped[int] = mapped_column(default=20_000)
    # approve_all: every public action waits for the founder. approve_new: only new kinds of
    # content wait. auto: the earned autonomy level decides.
    approval_mode: Mapped[str] = mapped_column(String(20), default="approve_all")
    promotion_threshold: Mapped[int] = mapped_column(Integer, default=50)
    # Founder caps per action type: {"gtm.publish_post": "ask_first"}
    level_caps: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    # Modules currently in "conditions changed" mode: {"gtm": "2026-11-23T06:00:00Z"}
    degraded_modules: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")


class AutonomyState(TimestampMixin, Base):
    """How much one kind of action has earned, for one business."""

    __tablename__ = "autonomy_states"

    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True)
    action_type: Mapped[str] = mapped_column(String(100), primary_key=True)
    level: Mapped[int] = mapped_column(SmallInteger)
    clean_streak: Mapped[int] = mapped_column(Integer, default=0)
    total_runs: Mapped[int] = mapped_column(Integer, default=0)
    total_edits: Mapped[int] = mapped_column(Integer, default=0)
    total_rejections: Mapped[int] = mapped_column(Integer, default=0)
    total_reversals: Mapped[int] = mapped_column(Integer, default=0)
    promotion_pending: Mapped[bool] = mapped_column(default=False)
    pack_version: Mapped[str | None] = mapped_column(String(30))
    last_incident_at: Mapped[datetime | None]
    last_promoted_at: Mapped[datetime | None]
    last_demoted_at: Mapped[datetime | None]
