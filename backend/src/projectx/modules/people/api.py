"""People endpoints: employees, leave and payroll. Admin role: this is personal and pay data."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select

from projectx.api.deps import BusinessDep
from projectx.kernel.errors import NotFound
from projectx.modules.people import service as people
from projectx.modules.people.models import Employee, LeaveRequest, PayrollRun
from projectx.spine.ledger.service import Actor

router = APIRouter(prefix="/api/v1/b/{business_id}/people", tags=["people"])


class HireIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    role: str = Field(min_length=1, max_length=120)
    team: str = Field(default="general", max_length=60)
    start_date: date
    monthly_salary_minor: int = Field(ge=0)
    manager_id: uuid.UUID | None = None


class LastDayIn(BaseModel):
    last_day: date


class LeaveIn(BaseModel):
    start_date: date
    end_date: date
    kind: str = Field(default="annual", pattern="^(annual|sick|unpaid|other)$")


async def _admin(ctx: BusinessDep) -> None:
    ctx.require("admin")


async def _employee(ctx: BusinessDep, employee_id: uuid.UUID) -> Employee:
    e = await ctx.session.get(Employee, employee_id, with_for_update=True)
    if e is None:
        raise NotFound("Employee not found")
    return e


@router.get("/employees")
async def employees(ctx: BusinessDep) -> dict[str, Any]:
    await _admin(ctx)
    rows = (await ctx.session.execute(select(Employee).order_by(Employee.name))).scalars()
    return {"items": [people.employee_view(e) for e in rows]}


@router.post("/employees", status_code=201)
async def hire(body: HireIn, ctx: BusinessDep) -> dict[str, Any]:
    await _admin(ctx)
    e = await people.hire(ctx.session, ctx.business, **body.model_dump(), actor=Actor.user(ctx.user.id))
    return people.employee_view(e)


@router.post("/employees/{employee_id}/last-day")
async def last_day(employee_id: uuid.UUID, body: LastDayIn, ctx: BusinessDep) -> dict[str, Any]:
    await _admin(ctx)
    e = await _employee(ctx, employee_id)
    await people.set_last_day(ctx.session, ctx.business, e, body.last_day, actor=Actor.user(ctx.user.id))
    return people.employee_view(e)


@router.post("/employees/{employee_id}/leave", status_code=201)
async def leave(employee_id: uuid.UUID, body: LeaveIn, ctx: BusinessDep) -> dict[str, Any]:
    e = await _employee(ctx, employee_id)
    if e.user_id != ctx.user.id:
        await _admin(ctx)
    req = await people.request_leave(
        ctx.session,
        ctx.business,
        e,
        start=body.start_date,
        end=body.end_date,
        kind=body.kind,
        actor=Actor.user(ctx.user.id),
    )
    return {
        "id": str(req.id),
        "days": req.days,
        "status": req.status,
        "decision_id": str(req.decision_id) if req.decision_id else None,
    }


@router.get("/leave")
async def leave_list(ctx: BusinessDep) -> dict[str, Any]:
    await _admin(ctx)
    rows = (
        await ctx.session.execute(select(LeaveRequest).order_by(LeaveRequest.start_date.desc()).limit(200))
    ).scalars()
    return {
        "items": [
            {
                "id": str(r.id),
                "employee_id": str(r.employee_id),
                "start_date": r.start_date.isoformat(),
                "end_date": r.end_date.isoformat(),
                "days": r.days,
                "kind": r.kind,
                "status": r.status,
            }
            for r in rows
        ]
    }


@router.get("/payroll")
async def payroll(ctx: BusinessDep) -> dict[str, Any]:
    await _admin(ctx)
    rows = (await ctx.session.execute(select(PayrollRun).order_by(PayrollRun.period.desc()).limit(36))).scalars()
    return {"items": [people.run_view(r) for r in rows]}
