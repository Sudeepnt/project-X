"""Legal and admin tables: contracts and their obligations, company assets (domains, software, devices), access."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class LegalSettings(TimestampMixin, Base):
    __tablename__ = "legal_settings"

    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True)
    # Accounts and equipment every new person needs; each becomes a set-up task and later a removal task.
    standard_access: Mapped[list[Any]] = mapped_column(default=lambda: ["Email", "Team chat", "Shared drive", "Laptop"])
    contract_min_value_minor: Mapped[int] = mapped_column(default=0)  # won deals at or above this get an agreement
    renewal_notice_days: Mapped[int] = mapped_column(Integer, default=14)
    templates: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")  # overrides by contract kind


class Contract(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "legal_contracts"
    __table_args__ = (
        Index("uq_legal_contracts_source", "business_id", "source_key", unique=True),
        Index("ix_legal_contracts_dates", "business_id", "status", "end_date"),
    )

    kind: Mapped[str] = mapped_column(String(12))  # customer | supplier | employment | nda | other
    title: Mapped[str] = mapped_column(String(200))
    counterparty: Mapped[str] = mapped_column(String(200))
    contact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    employee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("people_employees.id", ondelete="SET NULL"))
    deal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("deals.id", ondelete="SET NULL"))
    source_key: Mapped[str] = mapped_column(String(120))
    value_minor: Mapped[int] = mapped_column(default=0)
    currency: Mapped[str] = mapped_column(String(3))
    start_date: Mapped[date | None]
    end_date: Mapped[date | None]
    auto_renews: Mapped[bool] = mapped_column(default=False)
    notice_days: Mapped[int] = mapped_column(Integer, default=30)
    status: Mapped[str] = mapped_column(String(12), default="draft")  # draft | active | expired | terminated
    body: Mapped[str] = mapped_column(Text, default="")
    decision_id: Mapped[uuid.UUID | None]
    signed_at: Mapped[datetime | None]


class Obligation(IdMixin, TenantMixin, Base):
    """Something a contract makes someone do by a date, quoted from the contract itself."""

    __tablename__ = "legal_obligations"
    __table_args__ = (Index("ix_legal_obligations_due", "business_id", "due_date"),)

    contract_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("legal_contracts.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(12))  # payment | delivery | notice | renewal | other
    text: Mapped[str] = mapped_column(String(500))
    evidence: Mapped[str] = mapped_column(Text)
    due_date: Mapped[date]
    reminder_id: Mapped[uuid.UUID | None]


class Asset(IdMixin, TenantMixin, TimestampMixin, Base):
    """What the business owns or rents: domains, software, devices, certificates."""

    __tablename__ = "admin_assets"
    __table_args__ = (Index("ix_admin_assets_renewal", "business_id", "status", "renews_on"),)

    kind: Mapped[str] = mapped_column(String(12))  # domain | software | device | certificate | other
    name: Mapped[str] = mapped_column(String(200))
    vendor: Mapped[str] = mapped_column(String(120), default="")
    owner_employee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("people_employees.id", ondelete="SET NULL"))
    cost_minor: Mapped[int] = mapped_column(default=0)
    currency: Mapped[str] = mapped_column(String(3))
    billing: Mapped[str] = mapped_column(String(8), default="yearly")  # monthly | yearly | once
    renews_on: Mapped[date | None]
    seats: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(10), default="active")  # active | cancelled
    announced_for: Mapped[date | None]  # the renewal date Finance was last told about


class Access(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "admin_access"
    __table_args__ = (Index("uq_admin_access", "business_id", "employee_id", "system", unique=True),)

    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("people_employees.id", ondelete="CASCADE"))
    system: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(12), default="to_grant")  # to_grant | granted | to_revoke | revoked
    decision_id: Mapped[uuid.UUID | None]
    granted_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
