"""People tables: employees, leave, payroll runs and payslips, settings."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from projectx.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class PeopleSettings(TimestampMixin, Base):
    __tablename__ = "people_settings"

    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True)
    payroll_day: Mapped[int] = mapped_column(Integer, default=20)  # the run is prepared on this day each month
    pay_day: Mapped[int] = mapped_column(Integer, default=28)
    withholding_bp: Mapped[int] = mapped_column(
        Integer, default=1000
    )  # simple flat withholding; set by your accountant
    leave_days_per_year: Mapped[int] = mapped_column(Integer, default=20)
    max_off_per_team: Mapped[int] = mapped_column(Integer, default=1)  # others already off before leave needs a person


class Employee(IdMixin, TenantMixin, TimestampMixin, Base):
    """Personal data lives here, never in the ledger."""

    __tablename__ = "people_employees"
    __table_args__ = (Index("ix_people_employees_status", "business_id", "status"),)

    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320))
    role: Mapped[str] = mapped_column(String(120))
    team: Mapped[str] = mapped_column(String(60), default="general")
    manager_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("people_employees.id", ondelete="SET NULL"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    start_date: Mapped[date]
    end_date: Mapped[date | None]
    status: Mapped[str] = mapped_column(String(12), default="onboarding")  # onboarding | active | leaving | left
    monthly_salary_minor: Mapped[int] = mapped_column(default=0)
    currency: Mapped[str] = mapped_column(String(3))


class LeaveRequest(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "people_leave"
    __table_args__ = (Index("ix_people_leave_dates", "business_id", "start_date", "end_date"),)

    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("people_employees.id", ondelete="CASCADE"))
    start_date: Mapped[date]
    end_date: Mapped[date]
    days: Mapped[int] = mapped_column(Integer)  # working days (Mon to Fri)
    kind: Mapped[str] = mapped_column(String(10), default="annual")  # annual | sick | unpaid | other
    status: Mapped[str] = mapped_column(String(10), default="pending")  # pending | approved | declined | cancelled
    decision_id: Mapped[uuid.UUID | None]


class PayrollRun(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "people_payroll_runs"
    __table_args__ = (Index("uq_people_payroll_period", "business_id", "period", unique=True),)

    period: Mapped[str] = mapped_column(String(7))  # YYYY-MM
    period_start: Mapped[date]
    period_end: Mapped[date]  # inclusive
    pay_date: Mapped[date]
    currency: Mapped[str] = mapped_column(String(3))
    employees: Mapped[int] = mapped_column(Integer, default=0)
    gross_minor: Mapped[int] = mapped_column(default=0)
    withheld_minor: Mapped[int] = mapped_column(default=0)
    net_minor: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(10), default="draft")  # draft | approved | paid
    decision_id: Mapped[uuid.UUID | None]
    paid_at: Mapped[datetime | None]


class Payslip(IdMixin, TenantMixin, Base):
    __tablename__ = "people_payslips"
    __table_args__ = (Index("uq_people_payslip", "business_id", "run_id", "employee_id", unique=True),)

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("people_payroll_runs.id", ondelete="CASCADE"))
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("people_employees.id", ondelete="CASCADE"))
    days_employed: Mapped[int] = mapped_column(Integer)
    period_days: Mapped[int] = mapped_column(Integer)
    unpaid_days: Mapped[int] = mapped_column(Integer, default=0)
    gross_minor: Mapped[int]
    withheld_minor: Mapped[int]
    net_minor: Mapped[int]
    emailed_at: Mapped[datetime | None]
