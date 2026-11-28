"""Product tables: roadmap items, the customer feedback behind them, and releases."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class Item(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "product_items"
    __table_args__ = (Index("ix_product_items_rank", "business_id", "status", "score"),)

    title: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(10), default="feature")  # feature | bug | chore
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(10), default="idea")  # idea | planned | building | shipped | wont_do
    effort: Mapped[int] = mapped_column(Integer, default=3)  # 1 (hours) to 5 (weeks), set by a person
    requesters: Mapped[int] = mapped_column(Integer, default=0)
    value_minor: Mapped[int] = mapped_column(default=0)  # lifetime value of the customers who asked
    score: Mapped[float] = mapped_column(Float, default=0.0)
    spec: Mapped[str | None] = mapped_column(Text)
    spec_status: Mapped[str] = mapped_column(String(10), default="none")  # none | drafting | ready
    shipped_at: Mapped[datetime | None]


class FeedbackLink(IdMixin, TenantMixin, Base):
    __tablename__ = "product_feedback"
    __table_args__ = (Index("uq_product_feedback_ticket", "business_id", "ticket_id", unique=True),)

    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ops_tickets.id", ondelete="CASCADE"))
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("product_items.id", ondelete="CASCADE"))
    contact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Release(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "product_releases"

    version: Mapped[str] = mapped_column(String(40))
    notes: Mapped[str] = mapped_column(Text)
    item_ids: Mapped[list[Any]] = mapped_column(default=list)
    published_at: Mapped[datetime | None]
