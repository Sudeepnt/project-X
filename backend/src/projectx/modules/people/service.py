"""People at work: hiring and leaving, leave, and payroll, handing off to every department that needs to know.

- Hired → Legal drafts the employment contract, Admin sets up accounts and equipment (they subscribe).
- Leaving → Admin revokes access on the last day; the final month is paid pro rata automatically.
- Leave → checked by code against the allowance and how many teammates are already off; an approval card
  that can earn the right to go alone, or a person decides when a rule is bent.
- Payroll → computed by code on the payroll day (pro rata, unpaid leave, withholding), approved by a person,
  then Finance books it and proposes the payment (money out waits again); once paid, payslips go out.

The withholding here is a single configurable rate, not a tax engine: your accountant sets it.
"""

from __future__ import annotations

import calendar
import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.errors import NotFound, ValidationFailed
from projectx.kernel.money import format_money
from projectx.kernel.time import today_in, utcnow
from projectx.modules.people.models import Employee, LeaveRequest, PayrollRun, Payslip, PeopleSettings
from projectx.services import Services
from projectx.spine.bus import BusContext, on
from projectx.spine.decisions import service as decisions
from projectx.spine.decisions.executors import ExecutionContext, ExecutionResult, executor, undoer
from projectx.spine.ledger.events import EventData, register_event
from projectx.spine.ledger.service import Actor, append
from projectx.spine.lifecycle import queue_email
from projectx.spine.policy.actions import ActionType, register_action
from projectx.spine.policy.engine import Violation
from projectx.spine.policy.levels import Level
from projectx.spine.tenancy.models import Business

M = "people"
AGENT = Actor.agent("people")

APPROVE_LEAVE = register_action(
    ActionType(
        key="people.approve_leave",
        module=M,
        pack="people.leave",
        title="Approve leave",
        description="Approve time off that fits the allowance and the team.",
        default_level=Level.ASK_FIRST,
        max_level=Level.RUN,
        reversible=True,
        undo="Cancel the approved leave and tell the person.",
    )
)
APPROVE_PAYROLL = register_action(
    ActionType(
        key="people.approve_payroll",
        module=M,
        pack="people.payroll",
        title="Approve payroll",
        description="Approve this month's computed pay. Finance then books it and proposes the payment.",
        default_level=Level.ASK_FIRST,
        max_level=Level.ASK_FIRST,
        reversible=False,
        moves_money=True,
    )
)


class EmployeeChanged(EventData):
    employee_id: str
    status: str
    team: str
    start_date: str
    end_date: str | None = None


class LeaveChanged(EventData):
    leave_id: str
    employee_id: str
    status: str
    kind: str
    days: int


class PayrollChanged(EventData):
    run_id: str
    period: str
    status: str
    employees: int
    gross_minor: int
    withheld_minor: int
    net_minor: int
    currency: str
    pay_date: str


for _k, _d, _s in (
    ("people.employee.hired", "Someone was hired", EmployeeChanged),
    ("people.employee.started", "A new person started", EmployeeChanged),
    ("people.employee.leaving", "Someone's last day was set", EmployeeChanged),
    ("people.employee.left", "Someone left", EmployeeChanged),
    ("people.leave.requested", "Time off was requested", LeaveChanged),
    ("people.leave.approved", "Time off was approved", LeaveChanged),
    ("people.leave.cancelled", "Time off was cancelled", LeaveChanged),
    ("people.payroll.computed", "This month's payroll was computed", PayrollChanged),
    ("people.payroll.approved", "Payroll was approved", PayrollChanged),
    ("people.payroll.paid", "Payroll was paid", PayrollChanged),
):
    register_event(_k, _d, _s)


async def settings_for(session: AsyncSession, business_id: uuid.UUID) -> PeopleSettings:
    s = await session.get(PeopleSettings, business_id)
    if s is None:
        s = PeopleSettings(business_id=business_id)
        session.add(s)
        await session.flush()
    return s


def _emp(e: Employee) -> dict[str, Any]:
    return {
        "employee_id": str(e.id),
        "status": e.status,
        "team": e.team,
        "start_date": e.start_date.isoformat(),
        "end_date": e.end_date.isoformat() if e.end_date else None,
    }


async def _emit(session: AsyncSession, business_id: uuid.UUID, type_: str, e: Employee, actor: Actor) -> None:
    await append(session, business_id=business_id, type=type_, actor=actor, subject=("employee", e.id), data=_emp(e))


# ---------------------------------------------------------------- joining and leaving


async def hire(
    session: AsyncSession,
    business: Business,
    *,
    name: str,
    email: str,
    role: str,
    team: str,
    start_date: date,
    monthly_salary_minor: int,
    manager_id: uuid.UUID | None,
    actor: Actor,
) -> Employee:
    if monthly_salary_minor < 0:
        raise ValidationFailed("Salary can't be negative")
    e = Employee(
        business_id=business.id,
        name=name.strip()[:200],
        email=email.strip().lower()[:320],
        role=role.strip()[:120],
        team=team.strip().lower()[:60] or "general",
        manager_id=manager_id,
        start_date=start_date,
        status="active" if start_date <= today_in(business.timezone) else "onboarding",
        monthly_salary_minor=monthly_salary_minor,
        currency=business.currency,
    )
    session.add(e)
    await session.flush()
    await _emit(session, business.id, "people.employee.hired", e, actor)
    from projectx.spine.calendar.service import add_reminder

    first_day = _nine(e.start_date, business.timezone)
    if first_day > utcnow():
        await add_reminder(
            session,
            business,
            title=f"{e.name} starts as {e.role}",
            due_at=first_day,
            kind="task",
            all_day=True,
            notify_before_minutes=[3 * 1440, 0],
            source="agent",
            dedupe_key=f"start:{e.id}",
            actor=AGENT,
        )
    probation = _nine(e.start_date + timedelta(days=90), business.timezone)
    if probation > utcnow():
        await add_reminder(
            session,
            business,
            title=f"Probation review for {e.name}",
            due_at=probation,
            kind="deadline",
            all_day=True,
            notify_before_minutes=[7 * 1440],
            source="agent",
            dedupe_key=f"probation:{e.id}",
            actor=AGENT,
        )
    return e


def _nine(day: date, tz: str) -> Any:
    from datetime import datetime, time

    from projectx.kernel.time import zone

    return datetime.combine(day, time(9), tzinfo=zone(tz))


async def set_last_day(session: AsyncSession, business: Business, e: Employee, last_day: date, *, actor: Actor) -> None:
    if last_day < e.start_date:
        raise ValidationFailed("The last day can't be before the first")
    e.end_date = last_day
    e.status = "left" if last_day < today_in(business.timezone) else "leaving"
    await _emit(session, business.id, "people.employee.leaving", e, actor)
    if e.status == "left":
        await _emit(session, business.id, "people.employee.left", e, actor)


async def daily(services: Services, business_id: uuid.UUID) -> None:
    async with services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        if business is None:
            return
        today = today_in(business.timezone)
        for e in (
            await session.execute(select(Employee).where(Employee.status.in_(("onboarding", "leaving"))))
        ).scalars():
            if e.status == "onboarding" and e.start_date <= today:
                e.status = "active"
                await _emit(session, business_id, "people.employee.started", e, AGENT)
            elif e.status == "leaving" and e.end_date is not None and e.end_date < today:
                e.status = "left"
                await _emit(session, business_id, "people.employee.left", e, AGENT)
        settings = await settings_for(session, business_id)
        if today.day >= settings.payroll_day:
            await prepare_payroll(session, business, today.year, today.month, settings)


# ---------------------------------------------------------------- leave


def working_days(start: date, end: date) -> int:
    days, d = 0, start
    while d <= end:
        days += d.weekday() < 5
        d += timedelta(days=1)
    return days


async def request_leave(
    session: AsyncSession, business: Business, e: Employee, *, start: date, end: date, kind: str, actor: Actor
) -> LeaveRequest:
    if end < start:
        raise ValidationFailed("Leave ends after it starts")
    days = working_days(start, end)
    if days == 0:
        raise ValidationFailed("That range has no working days")
    settings = await settings_for(session, business.id)
    req = LeaveRequest(business_id=business.id, employee_id=e.id, start_date=start, end_date=end, days=days, kind=kind)
    session.add(req)
    await session.flush()
    await append(
        session,
        business_id=business.id,
        type="people.leave.requested",
        actor=actor,
        subject=("leave", req.id),
        data={"leave_id": str(req.id), "employee_id": str(e.id), "status": req.status, "kind": kind, "days": days},
    )
    violations: list[Violation] = []
    if kind == "annual":
        used = await session.scalar(
            select(func.coalesce(func.sum(LeaveRequest.days), 0)).where(
                LeaveRequest.employee_id == e.id,
                LeaveRequest.kind == "annual",
                LeaveRequest.status == "approved",
                func.extract("year", LeaveRequest.start_date) == start.year,
            )
        )
        if int(used or 0) + days > settings.leave_days_per_year:
            violations.append(
                Violation(
                    "over_allowance",
                    f"Uses {int(used or 0) + days} of {settings.leave_days_per_year} days.",
                    hard=False,
                )
            )
    if kind == "unpaid":
        violations.append(Violation("unpaid", "Unpaid leave reduces this month's pay.", hard=False))
    overlapping = await session.scalar(
        select(func.count(func.distinct(LeaveRequest.employee_id)))
        .join(Employee, Employee.id == LeaveRequest.employee_id)
        .where(
            Employee.team == e.team,
            LeaveRequest.employee_id != e.id,
            LeaveRequest.status == "approved",
            LeaveRequest.start_date <= end,
            LeaveRequest.end_date >= start,
        )
    )
    if int(overlapping or 0) >= settings.max_off_per_team:
        violations.append(
            Violation("team_cover", f"{overlapping} others on the {e.team} team are off then.", hard=False)
        )
    outcome = await decisions.propose(
        session,
        business.id,
        decisions.Proposal(
            action_type=APPROVE_LEAVE.key,
            title=f"{e.name}: {kind} leave {start:%d %b} to {end:%d %b} ({days} day{'s' if days != 1 else ''})",
            payload={"leave_id": str(req.id)},
            violations=tuple(violations),
            subject=("leave", req.id),
            requested_by="pack:people.leave",
            priority=40,
        ),
    )
    req.decision_id = outcome.decision_id
    return req


@executor(APPROVE_LEAVE.key)
async def _approve_leave(ctx: ExecutionContext) -> ExecutionResult:
    async with ctx.services.db.tenant(ctx.business_id) as session:
        req = await session.get(LeaveRequest, uuid.UUID(ctx.payload["leave_id"]), with_for_update=True)
        if req is None:
            raise NotFound("Leave request not found")
        req.status = "approved"
        await append(
            session,
            business_id=ctx.business_id,
            type="people.leave.approved",
            actor=AGENT,
            subject=("leave", req.id),
            data={
                "leave_id": str(req.id),
                "employee_id": str(req.employee_id),
                "status": "approved",
                "kind": req.kind,
                "days": req.days,
            },
        )
        return ExecutionResult(data={"leave_id": str(req.id)}, undo_payload={"leave_id": str(req.id)})


@undoer(APPROVE_LEAVE.key)
async def _cancel_leave(ctx: ExecutionContext) -> dict[str, Any]:
    async with ctx.services.db.tenant(ctx.business_id) as session:
        req = await session.get(LeaveRequest, uuid.UUID(ctx.payload["leave_id"]), with_for_update=True)
        assert req is not None
        req.status = "cancelled"
        await append(
            session,
            business_id=ctx.business_id,
            type="people.leave.cancelled",
            actor=AGENT,
            subject=("leave", req.id),
            data={
                "leave_id": str(req.id),
                "employee_id": str(req.employee_id),
                "status": "cancelled",
                "kind": req.kind,
                "days": req.days,
            },
        )
        return {"cancelled": str(req.id)}


# ---------------------------------------------------------------- payroll


def payslip_for(
    salary_minor: int,
    start: date,
    end: date | None,
    period_start: date,
    period_end: date,
    unpaid_days: int,
    withholding_bp: int,
) -> dict[str, int]:
    """Pay for one person and one month, in integers: pro rata by calendar days, minus unpaid working days."""
    period_days = (period_end - period_start).days + 1
    first, last = max(start, period_start), min(end or period_end, period_end)
    days_employed = max((last - first).days + 1, 0)
    prorated = salary_minor * days_employed // period_days
    workdays = working_days(period_start, period_end)
    deduction = salary_minor * unpaid_days // workdays if workdays else 0
    gross = max(prorated - deduction, 0)
    withheld = (gross * withholding_bp + 5000) // 10000
    return {
        "days_employed": days_employed,
        "period_days": period_days,
        "unpaid_days": unpaid_days,
        "gross_minor": gross,
        "withheld_minor": withheld,
        "net_minor": gross - withheld,
    }


async def prepare_payroll(
    session: AsyncSession, business: Business, year: int, month: int, settings: PeopleSettings
) -> PayrollRun | None:
    period = f"{year:04d}-{month:02d}"
    if await session.scalar(select(PayrollRun.id).where(PayrollRun.period == period)):
        return None
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    staff = list(
        (
            await session.execute(
                select(Employee).where(
                    Employee.start_date <= end,
                    or_(Employee.end_date.is_(None), Employee.end_date >= start),
                    Employee.monthly_salary_minor > 0,
                )
            )
        ).scalars()
    )
    if not staff:
        return None
    run = PayrollRun(
        business_id=business.id,
        period=period,
        period_start=start,
        period_end=end,
        pay_date=date(year, month, min(settings.pay_day, end.day)),
        currency=business.currency,
    )
    session.add(run)
    await session.flush()
    unpaid = await session.execute(
        select(LeaveRequest).where(
            LeaveRequest.kind == "unpaid",
            LeaveRequest.status == "approved",
            LeaveRequest.start_date <= end,
            LeaveRequest.end_date >= start,
        )
    )
    unpaid_by: dict[uuid.UUID, int] = {}
    for req in unpaid.scalars():
        unpaid_by[req.employee_id] = unpaid_by.get(req.employee_id, 0) + working_days(
            max(req.start_date, start), min(req.end_date, end)
        )
    for e in staff:
        slip = payslip_for(
            e.monthly_salary_minor,
            e.start_date,
            e.end_date,
            start,
            end,
            unpaid_by.get(e.id, 0),
            settings.withholding_bp,
        )
        session.add(Payslip(business_id=business.id, run_id=run.id, employee_id=e.id, **slip))
        run.gross_minor += slip["gross_minor"]
        run.withheld_minor += slip["withheld_minor"]
        run.net_minor += slip["net_minor"]
        run.employees += 1
    await session.flush()
    await _run_event(session, business.id, "people.payroll.computed", run, AGENT)
    outcome = await decisions.propose(
        session,
        business.id,
        decisions.Proposal(
            action_type=APPROVE_PAYROLL.key,
            title=f"Approve {period} payroll: {format_money(run.net_minor, run.currency)} to {run.employees} people",
            summary=f"Gross {format_money(run.gross_minor, run.currency)}, "
            f"withheld {format_money(run.withheld_minor, run.currency)}. "
            f"Pay day {run.pay_date:%d %b}.",
            payload={"run_id": str(run.id)},
            amount_minor=run.net_minor,
            currency=run.currency,
            why_now="It's payroll day; approving now leaves time to pay on pay day.",
            if_no="Nobody is paid until it is approved.",
            subject=("payroll_run", run.id),
            requested_by="pack:people.payroll",
            priority=10,
        ),
    )
    run.decision_id = outcome.decision_id
    return run


async def _run_event(session: AsyncSession, business_id: uuid.UUID, type_: str, run: PayrollRun, actor: Actor) -> None:
    await append(
        session,
        business_id=business_id,
        type=type_,
        actor=actor,
        subject=("payroll_run", run.id),
        data={
            "run_id": str(run.id),
            "period": run.period,
            "status": run.status,
            "employees": run.employees,
            "gross_minor": run.gross_minor,
            "withheld_minor": run.withheld_minor,
            "net_minor": run.net_minor,
            "currency": run.currency,
            "pay_date": run.pay_date.isoformat(),
        },
    )


@executor(APPROVE_PAYROLL.key)
async def _approve_payroll(ctx: ExecutionContext) -> ExecutionResult:
    async with ctx.services.db.tenant(ctx.business_id) as session:
        run = await session.get(PayrollRun, uuid.UUID(ctx.payload["run_id"]), with_for_update=True)
        if run is None:
            raise NotFound("Payroll run not found")
        if run.status == "draft":
            run.status = "approved"
            await _run_event(session, ctx.business_id, "people.payroll.approved", run, AGENT)
        return ExecutionResult(
            data={"run_id": str(run.id), "period": run.period}, summary=f"Approved {run.period} payroll."
        )


@on("finance.bill.paid", "people.send_payslips", when=lambda d: d.get("category") == "payroll")
async def on_payroll_paid(ctx: BusContext) -> dict[str, Any] | None:
    from projectx.modules.finance.models import Bill

    async with ctx.services.db.tenant(ctx.business_id) as session:
        bill = await session.get(Bill, uuid.UUID(ctx.data["bill_id"]))
        if bill is None or not bill.source_key.startswith("payroll:"):
            return {"skipped": "not a payroll bill"}
        run = await session.get(PayrollRun, uuid.UUID(bill.source_key.split(":", 1)[1]), with_for_update=True)
        business = await session.get(Business, ctx.business_id)
        if run is None or business is None or run.status == "paid":
            return {"skipped": "already paid"}
        run.status, run.paid_at = "paid", utcnow()
        await _run_event(session, ctx.business_id, "people.payroll.paid", run, AGENT)
        sent = 0
        rows = await session.execute(
            select(Payslip, Employee).join(Employee, Employee.id == Payslip.employee_id).where(Payslip.run_id == run.id)
        )
        for slip, e in rows.all():
            if slip.emailed_at or not e.email:
                continue
            text = (
                f"Hello {e.name.split()[0]},\n\nYour payslip for {run.period} from {business.name}:\n"
                f"Days employed: {slip.days_employed} of {slip.period_days}\n"
                + (f"Unpaid leave days: {slip.unpaid_days}\n" if slip.unpaid_days else "")
                + f"Gross: {format_money(slip.gross_minor, run.currency)}\n"
                f"Withheld: {format_money(slip.withheld_minor, run.currency)}\n"
                f"Net paid: {format_money(slip.net_minor, run.currency)}\n"
            )
            await queue_email(
                session,
                to=e.email,
                subject=f"Payslip {run.period}",
                html="<p>" + text.replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br>") + "</p>",
                text=text,
                business_id=ctx.business_id,
            )
            slip.emailed_at = utcnow()
            sent += 1
        return {"payslips": sent}


# ---------------------------------------------------------------- views and numbers


def employee_view(e: Employee) -> dict[str, Any]:
    return {
        "id": str(e.id),
        "name": e.name,
        "email": e.email,
        "role": e.role,
        "team": e.team,
        "manager_id": str(e.manager_id) if e.manager_id else None,
        "start_date": e.start_date.isoformat(),
        "end_date": e.end_date.isoformat() if e.end_date else None,
        "status": e.status,
        "monthly_salary_minor": e.monthly_salary_minor,
        "currency": e.currency,
    }


def run_view(r: PayrollRun) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "period": r.period,
        "pay_date": r.pay_date.isoformat(),
        "employees": r.employees,
        "gross_minor": r.gross_minor,
        "withheld_minor": r.withheld_minor,
        "net_minor": r.net_minor,
        "currency": r.currency,
        "status": r.status,
        "decision_id": str(r.decision_id) if r.decision_id else None,
    }


async def numbers(session: AsyncSession, business_id: uuid.UUID) -> dict[str, Any]:
    today = date.today()
    headcount = await session.scalar(
        select(func.count()).select_from(Employee).where(Employee.status.in_(("active", "leaving")))
    )
    payroll = await session.scalar(
        select(func.coalesce(func.sum(Employee.monthly_salary_minor), 0)).where(
            Employee.status.in_(("active", "leaving", "onboarding"))
        )
    )
    off = await session.scalar(
        select(func.count())
        .select_from(LeaveRequest)
        .where(LeaveRequest.status == "approved", LeaveRequest.start_date <= today, LeaveRequest.end_date >= today)
    )
    return {"headcount": int(headcount or 0), "monthly_payroll_minor": int(payroll or 0), "off_today": int(off or 0)}
