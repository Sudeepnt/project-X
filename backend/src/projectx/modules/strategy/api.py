"""Strategy endpoints: the company overview, objectives and reviews."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from projectx.api.deps import BusinessDep, OwnerDep
from projectx.kernel.errors import NotFound, ValidationFailed
from projectx.modules.strategy import service as strategy
from projectx.modules.strategy.models import Objective, Review

router = APIRouter(prefix="/api/v1/b/{business_id}/strategy", tags=["strategy"])


class ObjectiveIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    metric: str = Field(pattern=r"^[a-z_]+\.[a-z0-9_]+$")
    target: float
    direction: str = Field(default="up", pattern="^(up|down)$")
    due_date: date


@router.get("/overview")
async def overview(ctx: BusinessDep) -> dict[str, Any]:
    return await strategy.overview(ctx.session, ctx.business)


@router.post("/objectives", status_code=201)
async def add_objective(body: ObjectiveIn, ctx: OwnerDep) -> dict[str, Any]:
    numbers = await strategy.company_numbers(ctx.session, ctx.business_id)
    start = strategy.metric_value(numbers, body.metric)
    if body.metric.split(".")[0] not in numbers:
        raise ValidationFailed(f"No department reports {body.metric}; pick one from the overview")
    o = Objective(business_id=ctx.business_id, start_value=start, last_value=start, **body.model_dump())
    ctx.session.add(o)
    await ctx.session.flush()
    return strategy.objective_view(o)


@router.delete("/objectives/{objective_id}", status_code=204)
async def drop_objective(objective_id: uuid.UUID, ctx: OwnerDep) -> None:
    o = await ctx.session.get(Objective, objective_id)
    if o is None:
        raise NotFound("Objective not found")
    o.status = "dropped"


@router.get("/reviews")
async def reviews(ctx: BusinessDep) -> dict[str, Any]:
    rows = (await ctx.session.execute(select(Review).order_by(Review.created_at.desc()).limit(26))).scalars()
    return {"items": [strategy.review_view(r) for r in rows]}
