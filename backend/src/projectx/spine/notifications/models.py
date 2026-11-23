from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TenantMixin


class Notification(IdMixin, TenantMixin, Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_feed", "business_id", "created_at"),)

    user_id: Mapped[uuid.UUID | None]  # None: every member of the business
    kind: Mapped[str] = mapped_column(String(50))
    level: Mapped[str] = mapped_column(String(10), default="info")  # info|success|warning|critical
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    read_at: Mapped[datetime | None]
