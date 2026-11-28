from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class Integration(IdMixin, TenantMixin, TimestampMixin, Base):
    """How one business uses one outside tool. Secrets are in the vault (`secret_id`); `settings` holds only
    what is safe to show (an account id, a from-address, model names, the last four characters of a key)."""

    __tablename__ = "integrations"
    __table_args__ = (Index("uq_integrations_tool", "business_id", "tool", unique=True),)

    tool: Mapped[str] = mapped_column(String(40))
    mode: Mapped[str] = mapped_column(String(10), default="off")  # own | platform | off
    secret_id: Mapped[uuid.UUID | None]
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    status: Mapped[str] = mapped_column(String(12), default="unverified")  # ok | pending | error | unverified
    last_error: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime | None]
