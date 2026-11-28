"""Network endpoints: partners, handshake links and meetings between linked businesses."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from projectx.api.deps import BusinessDep, EditorDep, OwnerDep
from projectx.kernel.errors import NotFound
from projectx.modules.network import service as network
from projectx.modules.network.models import HandshakeLink, HandshakeMeeting, Partner
from projectx.spine.ledger.service import Actor

router = APIRouter(prefix="/api/v1/b/{business_id}/network", tags=["network"])


class PartnerIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: str = Field(pattern="^(supplier|partner|referrer|investor|advisor|other)$")
    contact_id: uuid.UUID | None = None
    cadence_days: int | None = Field(default=None, ge=7, le=365)
    notes: str = Field(default="", max_length=5000)


class InviteIn(BaseModel):
    scopes: list[str] = Field(default_factory=lambda: ["availability"])


class AcceptIn(BaseModel):
    code: str = Field(min_length=10, max_length=100)
    grant: list[str] = Field(default_factory=lambda: ["availability"])


class MeetIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    duration_minutes: int = Field(default=30, ge=15, le=240)
    within_days: int = Field(default=7, ge=1, le=21)


@router.get("/partners")
async def partners(ctx: BusinessDep) -> dict[str, Any]:
    rows = (await ctx.session.execute(select(Partner).order_by(Partner.name))).scalars()
    return {"items": [network.partner_view(p) for p in rows]}


@router.post("/partners", status_code=201)
async def add_partner(body: PartnerIn, ctx: EditorDep) -> dict[str, Any]:
    p = Partner(business_id=ctx.business_id, **body.model_dump())
    ctx.session.add(p)
    await ctx.session.flush()
    return network.partner_view(p)


@router.post("/partners/{partner_id}/touched")
async def touched(partner_id: uuid.UUID, ctx: EditorDep) -> dict[str, Any]:
    p = await ctx.session.get(Partner, partner_id)
    if p is None:
        raise NotFound("Partner not found")
    p.last_contact_at = datetime.now().astimezone()
    return network.partner_view(p)


@router.post("/handshake/invites", status_code=201)
async def invite(body: InviteIn, ctx: OwnerDep) -> dict[str, str]:
    """A one-time code for another business on this platform. Shown once; only its hash is kept."""
    return {"code": await network.create_invite(ctx.session, ctx.business_id, body.scopes)}


@router.post("/handshake/accept", status_code=201)
async def accept(body: AcceptIn, ctx: OwnerDep) -> dict[str, Any]:
    link = await network.accept_invite(ctx.services, ctx.business_id, body.code, body.grant, Actor.user(ctx.user.id))
    return network.link_view(link)


@router.get("/handshake/links")
async def links(ctx: BusinessDep) -> dict[str, Any]:
    rows = (await ctx.session.execute(select(HandshakeLink).order_by(HandshakeLink.created_at))).scalars()
    return {"items": [network.link_view(link) for link in rows]}


@router.post("/handshake/links/{link_id}/revoke", status_code=204)
async def revoke(link_id: uuid.UUID, ctx: OwnerDep) -> None:
    await network.revoke(ctx.services, ctx.business_id, link_id, Actor.user(ctx.user.id))


@router.post("/handshake/links/{link_id}/meet")
async def meet(link_id: uuid.UUID, body: MeetIn, ctx: EditorDep) -> dict[str, Any]:
    return await network.propose_meeting(
        ctx.services,
        ctx.business_id,
        link_id,
        title=body.title,
        duration_minutes=body.duration_minutes,
        days=body.within_days,
        actor=Actor.user(ctx.user.id),
    )


@router.get("/handshake/meetings")
async def meetings(ctx: BusinessDep) -> dict[str, Any]:
    rows = (
        await ctx.session.execute(select(HandshakeMeeting).order_by(HandshakeMeeting.starts_at.desc()).limit(100))
    ).scalars()
    return {
        "items": [
            {
                "ref": str(m.ref),
                "role": m.role,
                "title": m.title,
                "starts_at": m.starts_at.isoformat(),
                "ends_at": m.ends_at.isoformat(),
                "status": m.status,
            }
            for m in rows
        ]
    }
