"""Finance tables: chart of accounts, journal (double entry), invoices, bills, tax filings, settings."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class FinanceSettings(TimestampMixin, Base):
    __tablename__ = "fin_settings"

    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True)
    invoice_prefix: Mapped[str] = mapped_column(String(12), default="INV-")
    next_invoice_no: Mapped[int] = mapped_column(Integer, default=1)
    payment_terms_days: Mapped[int] = mapped_column(Integer, default=14)
    tax_name: Mapped[str] = mapped_column(String(20), default="Tax")
    tax_rate_bp: Mapped[int] = mapped_column(Integer, default=0)  # basis points: 1800 = 18%
    tax_period: Mapped[str] = mapped_column(String(10), default="monthly")  # monthly | quarterly
    tax_due_day: Mapped[int] = mapped_column(Integer, default=20)  # day of the month after the period
    dunning_days: Mapped[list[Any]] = mapped_column(default=lambda: [1, 7, 14])  # days overdue to remind
    escalate_after_days: Mapped[int] = mapped_column(Integer, default=30)


class Account(IdMixin, TenantMixin, Base):
    __tablename__ = "fin_accounts"
    __table_args__ = (Index("uq_fin_accounts_code", "business_id", "code", unique=True),)

    code: Mapped[str] = mapped_column(String(10))
    name: Mapped[str] = mapped_column(String(100))
    type: Mapped[str] = mapped_column(String(10))  # asset | liability | equity | income | expense


class JournalEntry(IdMixin, TenantMixin, Base):
    """One balanced transaction. Entries are never edited; a mistake is fixed with a reversing entry."""

    __tablename__ = "fin_journal"
    __table_args__ = (
        Index("uq_fin_journal_source", "business_id", "source_type", "source_id", "purpose", unique=True),
        Index("ix_fin_journal_date", "business_id", "entry_date"),
    )

    entry_date: Mapped[date]
    memo: Mapped[str] = mapped_column(String(300))
    source_type: Mapped[str] = mapped_column(String(30))  # invoice | payment | bill | payroll | tax | manual
    source_id: Mapped[uuid.UUID]
    purpose: Mapped[str] = mapped_column(String(30))  # issue | payment | void | pay …
    currency: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class JournalLine(IdMixin, TenantMixin, Base):
    __tablename__ = "fin_journal_lines"
    __table_args__ = (
        Index("ix_fin_lines_account", "business_id", "account_id"),
        CheckConstraint(
            "debit_minor >= 0 AND credit_minor >= 0 AND (debit_minor = 0) <> (credit_minor = 0)", name="one_side"
        ),
    )

    entry_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("fin_journal.id", ondelete="CASCADE"))
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("fin_accounts.id", ondelete="RESTRICT"))
    debit_minor: Mapped[int] = mapped_column(default=0)
    credit_minor: Mapped[int] = mapped_column(default=0)


class Invoice(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "fin_invoices"
    __table_args__ = (
        Index("uq_fin_invoices_number", "business_id", "number", unique=True),
        Index("uq_fin_invoices_source", "business_id", "source_key", unique=True),
        Index("ix_fin_invoices_open", "business_id", "status", "due_date"),
    )

    number: Mapped[str] = mapped_column(String(30))
    contact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    deal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("deals.id", ondelete="SET NULL"))
    source_key: Mapped[str] = mapped_column(String(120))  # e.g. "deal:<id>" — one invoice per source
    currency: Mapped[str] = mapped_column(String(3))
    lines: Mapped[list[Any]] = mapped_column(default=list)  # [{description, quantity, unit_minor, amount_minor}]
    subtotal_minor: Mapped[int] = mapped_column(default=0)
    tax_rate_bp: Mapped[int] = mapped_column(Integer, default=0)
    tax_minor: Mapped[int] = mapped_column(default=0)
    total_minor: Mapped[int] = mapped_column(default=0)
    paid_minor: Mapped[int] = mapped_column(default=0)
    issue_date: Mapped[date | None]
    due_date: Mapped[date | None]
    # draft | sent | partially_paid | overdue | paid | void
    status: Mapped[str] = mapped_column(String(16), default="draft")
    reminders_sent: Mapped[list[Any]] = mapped_column(default=list)  # dunning steps done, e.g. [1, 7]
    escalated_at: Mapped[datetime | None]
    paid_at: Mapped[datetime | None]
    notes: Mapped[str] = mapped_column(Text, default="")


class Bill(IdMixin, TenantMixin, TimestampMixin, Base):
    """Money the business owes: suppliers, software, payroll, tax."""

    __tablename__ = "fin_bills"
    __table_args__ = (
        Index("uq_fin_bills_source", "business_id", "source_key", unique=True),
        Index("ix_fin_bills_open", "business_id", "status", "due_date"),
    )

    supplier: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(30), default="other")  # software | payroll | tax | rent | other
    source_key: Mapped[str] = mapped_column(String(120))
    currency: Mapped[str] = mapped_column(String(3))
    amount_minor: Mapped[int]
    due_date: Mapped[date]
    status: Mapped[str] = mapped_column(String(12), default="open")  # open | approved | paid | void
    decision_id: Mapped[uuid.UUID | None]
    paid_at: Mapped[datetime | None]
    notes: Mapped[str] = mapped_column(Text, default="")


class TaxFiling(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "fin_tax_filings"
    __table_args__ = (Index("uq_fin_tax_period", "business_id", "period_start", unique=True),)

    period_start: Mapped[date]
    period_end: Mapped[date]
    due_date: Mapped[date]
    tax_name: Mapped[str] = mapped_column(String(20))
    output_tax_minor: Mapped[int] = mapped_column(default=0)
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(12), default="ready")  # ready | filed
    decision_id: Mapped[uuid.UUID | None]
    filed_at: Mapped[datetime | None]
    reference: Mapped[str | None] = mapped_column(String(100))
