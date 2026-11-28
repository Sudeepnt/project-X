"""Product endpoints: roadmap items, specs, releases."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from projectx.api.deps import BusinessDep, EditorDep
from projectx.kernel.errors import NotFound
from projectx.modules.product import service as product
from projectx.modules.product.models import Item, Release
from projectx.spine.ledger.service import Actor

router = APIRouter(prefix="/api/v1/b/{business_id}/product", tags=["product"])


class ItemIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    kind: str = Field(default="feature", pattern="^(feature|bug|chore)$")
    description: str = Field(default="", max_length=10_000)
    effort: int = Field(default=3, ge=1, le=5)


class ItemPatch(BaseModel):
    status: str | None = Field(default=None, pattern="^(idea|planned|building|wont_do)$")
    effort: int | None = Field(default=None, ge=1, le=5)
    description: str | None = Field(default=None, max_length=10_000)


class ReleaseIn(BaseModel):
    version: str = Field(min_length=1, max_length=40)
    notes: str = Field(min_length=1, max_length=10_000)
    item_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)


@router.get("/items")
async def items(ctx: BusinessDep, status: str = "idea,planned,building") -> dict[str, Any]:
    rows = (
        await ctx.session.execute(
            select(Item).where(Item.status.in_(status.split(","))).order_by(Item.score.desc()).limit(300)
        )
    ).scalars()
    return {"items": [product.item_view(i) for i in rows]}


@router.post("/items", status_code=201)
async def add_item(body: ItemIn, ctx: EditorDep) -> dict[str, Any]:
    item = Item(business_id=ctx.business_id, **body.model_dump())
    ctx.session.add(item)
    await ctx.session.flush()
    await product.rerank(ctx.session, item)
    return product.item_view(item)


async def _item(ctx: BusinessDep, item_id: uuid.UUID) -> Item:
    item = await ctx.session.get(Item, item_id, with_for_update=True)
    if item is None:
        raise NotFound("Item not found")
    return item


@router.patch("/items/{item_id}")
async def update_item(item_id: uuid.UUID, body: ItemPatch, ctx: EditorDep) -> dict[str, Any]:
    item = await _item(ctx, item_id)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(item, k, v)
    await product.rerank(ctx.session, item)
    return product.item_view(item)


@router.post("/items/{item_id}/spec", status_code=202)
async def spec(item_id: uuid.UUID, ctx: EditorDep) -> dict[str, Any]:
    item = await _item(ctx, item_id)
    await product.request_spec(ctx.session, ctx.business_id, item)
    return product.item_view(item)


@router.post("/releases", status_code=201)
async def release(body: ReleaseIn, ctx: EditorDep) -> dict[str, Any]:
    r = await product.publish_release(
        ctx.session,
        ctx.business_id,
        version=body.version,
        notes=body.notes,
        item_ids=body.item_ids,
        actor=Actor.user(ctx.user.id),
    )
    return {"id": str(r.id), "version": r.version, "items": r.item_ids}


@router.get("/releases")
async def releases(ctx: BusinessDep) -> dict[str, Any]:
    rows = (await ctx.session.execute(select(Release).order_by(Release.created_at.desc()).limit(50))).scalars()
    return {"items": [{"id": str(r.id), "version": r.version, "notes": r.notes, "items": r.item_ids} for r in rows]}
