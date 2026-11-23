"""Businesses and their members."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, Literal

from fastapi import APIRouter, status
from pydantic import BaseModel, EmailStr, Field, HttpUrl
from sqlalchemy import select

from projectx.api.deps import AdminDep, AuthDep, BusinessDep, OwnerDep, ServicesDep, SessionDep
from projectx.kernel.errors import Conflict, NotFound, ValidationFailed
from projectx.spine.billing.plans import check_quota
from projectx.spine.identity.models import User
from projectx.spine.identity.service import issue_email_token
from projectx.spine.ledger.service import Actor, append
from projectx.spine.lifecycle import queue_email
from projectx.spine.mail import render
from projectx.spine.tenancy.models import Business, Membership
from projectx.spine.tenancy.service import business_view, create_business, list_businesses

router = APIRouter(prefix="/api/v1", tags=["businesses"])

BusinessModel = Literal["media", "service", "software", "ecommerce", "creator", "other"]


class BusinessIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    website: HttpUrl | None = None
    industry: str | None = Field(default=None, max_length=80)
    business_model: BusinessModel = "other"
    description: str | None = Field(default=None, max_length=1000)
    country: str | None = Field(default=None, min_length=2, max_length=2)
    timezone: str = "UTC"
    currency: str = Field(default="USD", min_length=3, max_length=3)
    practice_mode: bool = False


class BusinessPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    website: HttpUrl | None = None
    industry: str | None = Field(default=None, max_length=80)
    business_model: BusinessModel | None = None
    description: str | None = Field(default=None, max_length=1000)
    country: str | None = Field(default=None, min_length=2, max_length=2)
    timezone: str | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    practice_mode: bool | None = None
    onboarding: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None


@router.get("/businesses")
async def my_businesses(auth: AuthDep, session: SessionDep) -> list[dict[str, Any]]:
    return [business_view(b, role) for b, role in await list_businesses(session, auth.user.id)]


@router.post("/businesses", status_code=status.HTTP_201_CREATED)
async def new_business(body: BusinessIn, auth: AuthDep, session: SessionDep, services: ServicesDep) -> dict[str, Any]:
    owner = await session.get(User, auth.user.id)
    assert owner is not None
    business = await create_business(
        session,
        owner=owner,
        name=body.name,
        website=str(body.website) if body.website else None,
        industry=body.industry,
        business_model=body.business_model,
        country=body.country.upper() if body.country else None,
        timezone=body.timezone,
        currency=body.currency,
        description=body.description,
        practice_mode=body.practice_mode,
        plan=services.settings.new_business_plan,
    )
    return business_view(business, "owner")


@router.get("/b/{business_id}")
async def get_business(ctx: BusinessDep) -> dict[str, Any]:
    return business_view(ctx.business, ctx.role)


@router.patch("/b/{business_id}")
async def update_business(body: BusinessPatch, ctx: AdminDep) -> dict[str, Any]:
    from projectx.kernel.time import is_valid_timezone

    b = ctx.business
    data = body.model_dump(exclude_unset=True)
    if "timezone" in data and data["timezone"] and not is_valid_timezone(data["timezone"]):
        raise ValidationFailed("Unknown timezone", extra={"field": "timezone"})
    for key, raw in data.items():
        value = raw
        if key == "website":
            value = str(raw) if raw else None
        elif key in ("country", "currency") and raw:
            value = raw.upper()
        elif key in ("onboarding", "settings"):
            value = {**getattr(b, key), **(raw or {})}
        setattr(b, key, value)
    await ctx.session.flush()
    return business_view(b, ctx.role)


class InviteIn(BaseModel):
    email: EmailStr
    role: Literal["admin", "editor", "viewer"] = "editor"


@router.get("/b/{business_id}/members")
async def members(ctx: BusinessDep) -> list[dict[str, Any]]:
    rows = await ctx.session.execute(
        select(User, Membership.role)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.business_id == ctx.business_id)
        .order_by(Membership.created_at)
    )
    return [{"user_id": str(u.id), "name": u.name, "email": u.email, "role": role} for u, role in rows.all()]


@router.post("/b/{business_id}/members/invite", status_code=status.HTTP_202_ACCEPTED)
async def invite(body: InviteIn, ctx: AdminDep) -> dict[str, Any]:
    await check_quota(ctx.session, ctx.business, "seats")
    settings = ctx.services.settings
    token = await issue_email_token(
        ctx.session,
        user_id=None,
        purpose="invite",
        ttl=timedelta(days=7),
        payload={"business_id": str(ctx.business_id), "role": body.role},
    )
    link = f"{settings.web_url}/invite?token={token}"
    await queue_email(
        ctx.session,
        to=str(body.email),
        subject=f"Join {ctx.business.name} on {settings.app_name}",
        html=render(
            "email/action.html",
            title=f"Join {ctx.business.name}",
            name="there",
            body=f"{ctx.user.name} invited you to help run {ctx.business.name}.",
            action_label="Accept invitation",
            action_url=link,
            app_name=settings.app_name,
        ),
        text=f"{ctx.user.name} invited you to {ctx.business.name}: {link}",
        business_id=ctx.business_id,
    )
    return {"invited": True}


class RoleIn(BaseModel):
    role: Literal["admin", "editor", "viewer"]


@router.patch("/b/{business_id}/members/{user_id}")
async def change_role(user_id: uuid.UUID, body: RoleIn, ctx: OwnerDep) -> dict[str, Any]:
    membership = await ctx.session.get(Membership, (user_id, ctx.business_id))
    if membership is None:
        raise NotFound("Member not found")
    if membership.role == "owner":
        raise Conflict("The owner's role can't be changed here.")
    membership.role = body.role
    return {"user_id": str(user_id), "role": body.role}


@router.delete("/b/{business_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(user_id: uuid.UUID, ctx: AdminDep) -> None:
    membership = await ctx.session.get(Membership, (user_id, ctx.business_id))
    if membership is None:
        raise NotFound("Member not found")
    if membership.role == "owner":
        raise Conflict("The owner can't be removed.")
    await ctx.session.delete(membership)
    await append(
        ctx.session,
        business_id=ctx.business_id,
        type="spine.member.removed",
        actor=Actor.user(ctx.user.id),
        data={"user_id": str(user_id)},
    )


@router.get("/b/{business_id}/public-profile")
async def public_profile(ctx: BusinessDep) -> dict[str, Any]:
    b: Business = ctx.business
    return {"slug": b.slug, "public_base": f"{ctx.services.settings.base_url}/p/{b.slug}"}
