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
from projectx.modules.strategy import ask as asking
from projectx.modules.strategy import service as strategy
from projectx.modules.strategy import simulation as sim
from projectx.modules.strategy.models import Objective, Review, SimulationRun

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


class AskIn(BaseModel):
    text: str = Field(min_length=3, max_length=2000)


@router.get("/simulate/baseline")
async def baseline(ctx: BusinessDep) -> dict[str, Any]:
    """What the simulation believes about the business today, and where each number came from."""
    b = await sim.calibrate(ctx.session, ctx.business)
    return {"currency": b.currency, "assumptions": [a.__dict__ for a in b.assumptions]}


@router.post("/simulate")
async def simulate(body: sim.Question, ctx: BusinessDep) -> dict[str, Any]:
    """Run exact settings: the scenario next to doing nothing, with goal probabilities."""
    return await asking.run(ctx.services, ctx.business_id, ctx.user.id, body)


@router.post("/simulate/ask")
async def simulate_ask(body: AskIn, ctx: BusinessDep) -> dict[str, Any]:
    """Ask in plain words: "what if I hire two salespeople?", "will we hit 10 lakh revenue by March?"."""
    return await asking.ask(ctx.services, ctx.business_id, ctx.user.id, body.text)


@router.get("/simulations")
async def simulations(ctx: BusinessDep) -> dict[str, Any]:
    rows = (
        await ctx.session.execute(select(SimulationRun).order_by(SimulationRun.created_at.desc()).limit(50))
    ).scalars()
    return {
        "items": [
            {
                "id": str(r.id),
                "asked": r.asked,
                "summary": r.summary,
                "question": r.question,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
    }


@router.get("/simulations/{run_id}")
async def simulation(run_id: uuid.UUID, ctx: BusinessDep) -> dict[str, Any]:
    r = await ctx.session.get(SimulationRun, run_id)
    if r is None:
        raise NotFound("Simulation not found")
    return {"id": str(r.id), "asked": r.asked, "summary": r.summary, "question": r.question, **r.result}
