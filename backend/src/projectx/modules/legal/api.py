"""Legal and admin endpoints: contracts, obligations, assets, access, settings."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from projectx.api.deps import BusinessDep, EditorDep, OwnerDep
from projectx.modules.legal import service as legal
from projectx.modules.legal.models import Access, Asset, Contract, Obligation

router = APIRouter(prefix="/api/v1/b/{business_id}", tags=["legal"])


class ContractIn(BaseModel):
    kind: str = Field(pattern="^(customer|supplier|employment|nda|other)$")
    title: str = Field(min_length=1, max_length=200)
    counterparty: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=20, max_length=200_000)
    value_minor: int = Field(default=0, ge=0)
    start_date: date | None = None
    end_date: date | None = None
    auto_renews: bool = False
    notice_days: int = Field(default=30, ge=0, le=365)


class AssetIn(BaseModel):
    kind: str = Field(pattern="^(domain|software|device|certificate|other)$")
    name: str = Field(min_length=1, max_length=200)
    vendor: str = Field(default="", max_length=120)
    cost_minor: int = Field(default=0, ge=0)
    billing: str = Field(default="yearly", pattern="^(monthly|yearly|once)$")
    renews_on: date | None = None
    seats: int | None = Field(default=None, ge=0)
    owner_employee_id: uuid.UUID | None = None


class SettingsIn(BaseModel):
    standard_access: list[str] | None = Field(default=None, max_length=40)
    contract_min_value_minor: int | None = Field(default=None, ge=0)
    renewal_notice_days: int | None = Field(default=None, ge=1, le=90)
    templates: dict[str, str] | None = None


@router.get("/legal/contracts")
async def contracts(ctx: BusinessDep, status: str | None = None) -> dict[str, Any]:
    stmt = select(Contract).order_by(Contract.created_at.desc()).limit(200)
    if status:
        stmt = stmt.where(Contract.status.in_(status.split(",")))
    return {"items": [legal.contract_view(c) for c in (await ctx.session.execute(stmt)).scalars()]}


@router.post("/legal/contracts", status_code=201)
async def upload(body: ContractIn, ctx: EditorDep) -> dict[str, Any]:
    """Add a signed contract; its dates become reminders and its obligations are read out in the background."""
    c = await legal.upload_contract(ctx.session, ctx.business, **body.model_dump())
    return legal.contract_view(c)


@router.get("/legal/obligations")
async def obligations(ctx: BusinessDep) -> dict[str, Any]:
    rows = (await ctx.session.execute(select(Obligation).order_by(Obligation.due_date).limit(500))).scalars()
    return {
        "items": [
            {
                "id": str(o.id),
                "contract_id": str(o.contract_id),
                "kind": o.kind,
                "text": o.text,
                "evidence": o.evidence,
                "due_date": o.due_date.isoformat(),
            }
            for o in rows
        ]
    }


@router.get("/admin/assets")
async def assets(ctx: BusinessDep) -> dict[str, Any]:
    rows = (await ctx.session.execute(select(Asset).order_by(Asset.renews_on))).scalars()
    return {"items": [legal.asset_view(a) for a in rows]}


@router.post("/admin/assets", status_code=201)
async def add_asset(body: AssetIn, ctx: EditorDep) -> dict[str, Any]:
    a = Asset(business_id=ctx.business_id, currency=ctx.business.currency, **body.model_dump())
    ctx.session.add(a)
    await ctx.session.flush()
    return legal.asset_view(a)


@router.get("/admin/access")
async def access(ctx: BusinessDep) -> dict[str, Any]:
    ctx.require("admin")
    rows = (await ctx.session.execute(select(Access).order_by(Access.created_at.desc()).limit(500))).scalars()
    return {
        "items": [
            {
                "id": str(a.id),
                "employee_id": str(a.employee_id),
                "system": a.system,
                "status": a.status,
                "decision_id": str(a.decision_id) if a.decision_id else None,
            }
            for a in rows
        ]
    }


@router.put("/legal/settings")
async def put_settings(body: SettingsIn, ctx: OwnerDep) -> dict[str, Any]:
    s = await legal.settings_for(ctx.session, ctx.business_id)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(s, k, v)
    return {k: getattr(s, k) for k in SettingsIn.model_fields}
