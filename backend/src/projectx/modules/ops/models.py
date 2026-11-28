"""Operations tables: support tickets, help articles, delivery projects and their tasks."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import Computed, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class Ticket(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "ops_tickets"
    __table_args__ = (Index("ix_ops_tickets_queue", "business_id", "status", "sla_due_at"),)

    contact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)  # personal data: erased with the contact
    channel: Mapped[str] = mapped_column(String(20), default="email")
    status: Mapped[str] = mapped_column(String(10), default="open")  # open | waiting | solved
    priority: Mapped[str] = mapped_column(String(10), default="normal")  # urgent | high | normal | low
    kind: Mapped[str] = mapped_column(String(20), default="question")  # question | problem | feature | bug | billing
    sentiment: Mapped[str] = mapped_column(String(10), default="neutral")
    summary: Mapped[str] = mapped_column(String(300), default="")
    sla_due_at: Mapped[datetime | None]
    sla_warned: Mapped[bool] = mapped_column(default=False)
    first_response_at: Mapped[datetime | None]
    solved_at: Mapped[datetime | None]
    reply_draft: Mapped[str | None] = mapped_column(Text)
    article_id: Mapped[uuid.UUID | None]
    product_item_id: Mapped[uuid.UUID | None]
    decision_id: Mapped[uuid.UUID | None]
    csat: Mapped[int | None] = mapped_column(Integer)  # 1 to 5


class Article(IdMixin, TenantMixin, TimestampMixin, Base):
    """A help article: the only source the assistant may answer customers from."""

    __tablename__ = "ops_articles"
    __table_args__ = (Index("ix_ops_articles_search", "search", postgresql_using="gin"),)

    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    published: Mapped[bool] = mapped_column(default=True)
    search: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', coalesce(title, '') || ' ' || coalesce(body, ''))", persisted=True)
    )


class Project(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "ops_projects"
    __table_args__ = (Index("uq_ops_projects_deal", "business_id", "deal_id", unique=True),)

    deal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("deals.id", ondelete="SET NULL"))
    contact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(10), default="active")  # active | on_hold | done
    hold_reason: Mapped[str | None] = mapped_column(String(300))
    billing: Mapped[str] = mapped_column(String(12), default="full")  # full | milestones
    currency: Mapped[str] = mapped_column(String(3))
    started_on: Mapped[date]
    due_on: Mapped[date | None]
    kickoff_event_id: Mapped[uuid.UUID | None]
    done_at: Mapped[datetime | None]


class Task(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "ops_tasks"
    __table_args__ = (Index("ix_ops_tasks_project", "business_id", "project_id", "position"),)

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ops_projects.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200))
    position: Mapped[int] = mapped_column(Integer, default=0)
    due_on: Mapped[date | None]
    status: Mapped[str] = mapped_column(String(10), default="todo")  # todo | done
    milestone: Mapped[bool] = mapped_column(default=False)
    billable_minor: Mapped[int] = mapped_column(default=0)
    done_at: Mapped[datetime | None]
