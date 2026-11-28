"""Operations endpoints: tickets, help articles, projects and tasks."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from projectx.api.deps import BusinessDep, EditorDep
from projectx.kernel.errors import NotFound
from projectx.modules.ops import service as ops
from projectx.modules.ops.models import Article, Project, Task, Ticket
from projectx.spine.ledger.service import Actor

router = APIRouter(prefix="/api/v1/b/{business_id}/ops", tags=["ops"])


class TicketIn(BaseModel):
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=20_000)
    contact_id: uuid.UUID | None = None
    channel: str = Field(default="email", max_length=20)


class ArticleIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=50_000)
    published: bool = True


class CsatIn(BaseModel):
    score: int = Field(ge=1, le=5)


@router.get("/tickets")
async def list_tickets(ctx: BusinessDep, status: str = "open,waiting") -> dict[str, Any]:
    rows = (
        await ctx.session.execute(
            select(Ticket).where(Ticket.status.in_(status.split(","))).order_by(Ticket.sla_due_at).limit(200)
        )
    ).scalars()
    return {"items": [ops.ticket_view(t) for t in rows]}


@router.post("/tickets", status_code=201)
async def new_ticket(body: TicketIn, ctx: EditorDep) -> dict[str, Any]:
    t = await ops.open_ticket(
        ctx.session,
        ctx.business,
        subject=body.subject,
        body=body.body,
        contact_id=body.contact_id,
        channel=body.channel,
        actor=Actor.user(ctx.user.id),
    )
    return ops.ticket_view(t)


async def _ticket(ctx: BusinessDep, ticket_id: uuid.UUID) -> Ticket:
    t = await ctx.session.get(Ticket, ticket_id, with_for_update=True)
    if t is None:
        raise NotFound("Ticket not found")
    return t


@router.get("/tickets/{ticket_id}")
async def get_ticket(ticket_id: uuid.UUID, ctx: BusinessDep) -> dict[str, Any]:
    return ops.ticket_view(await _ticket(ctx, ticket_id))


@router.post("/tickets/{ticket_id}/solve")
async def solve(ticket_id: uuid.UUID, ctx: EditorDep) -> dict[str, Any]:
    t = await _ticket(ctx, ticket_id)
    await ops.solve(ctx.session, ctx.business_id, t, actor=Actor.user(ctx.user.id))
    return ops.ticket_view(t)


@router.post("/tickets/{ticket_id}/csat")
async def csat(ticket_id: uuid.UUID, body: CsatIn, ctx: EditorDep) -> dict[str, Any]:
    t = await _ticket(ctx, ticket_id)
    t.csat = body.score
    return ops.ticket_view(t)


@router.get("/articles")
async def articles(ctx: BusinessDep) -> dict[str, Any]:
    rows = (await ctx.session.execute(select(Article).order_by(Article.title))).scalars()
    return {"items": [{"id": str(a.id), "title": a.title, "body": a.body, "published": a.published} for a in rows]}


@router.post("/articles", status_code=201)
async def add_article(body: ArticleIn, ctx: EditorDep) -> dict[str, Any]:
    a = Article(business_id=ctx.business_id, title=body.title, body=body.body, published=body.published)
    ctx.session.add(a)
    await ctx.session.flush()
    return {"id": str(a.id), "title": a.title, "body": a.body, "published": a.published}


@router.get("/projects")
async def projects(ctx: BusinessDep, status: str = "active,on_hold") -> dict[str, Any]:
    rows = list(
        (
            await ctx.session.execute(
                select(Project).where(Project.status.in_(status.split(","))).order_by(Project.started_on.desc())
            )
        ).scalars()
    )
    tasks = list((await ctx.session.execute(select(Task).where(Task.project_id.in_([p.id for p in rows])))).scalars())
    return {"items": [ops.project_view(p, [t for t in tasks if t.project_id == p.id]) for p in rows]}


@router.post("/tasks/{task_id}/done")
async def task_done(task_id: uuid.UUID, ctx: EditorDep) -> dict[str, Any]:
    task = await ctx.session.get(Task, task_id, with_for_update=True)
    if task is None:
        raise NotFound("Task not found")
    project = await ops.complete_task(ctx.session, ctx.business, task, actor=Actor.user(ctx.user.id))
    tasks = list((await ctx.session.execute(select(Task).where(Task.project_id == project.id))).scalars())
    return ops.project_view(project, tasks)
