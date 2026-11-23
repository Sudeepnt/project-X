"""Managing tracked links, landing pages and promo codes."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from projectx.api.deps import BusinessDep, EditorDep
from projectx.kernel.errors import Conflict, NotFound, ValidationFailed
from projectx.modules.gtm.growth.landing import TEMPLATES, create_page, page_view, publish_page
from projectx.modules.gtm.growth.links import create_link, link_view
from projectx.modules.gtm.models import LandingPage, LandingVariant, PromoCode, TrackedLink

router = APIRouter(prefix="/api/v1/b/{business_id}/gtm", tags=["gtm: growth"])

PageKind = Literal["newsletter", "lead_magnet", "consult", "waitlist", "webinar", "media_kit", "generic"]


class LinkIn(BaseModel):
    destination: str = Field(min_length=8, max_length=2000)
    platform: str | None = Field(default=None, max_length=30)
    campaign_id: uuid.UUID | None = None
    label: str | None = Field(default=None, max_length=200)
    utm: dict[str, str] | None = None


@router.get("/links")
async def links(ctx: BusinessDep, campaign_id: uuid.UUID | None = None) -> dict[str, Any]:
    stmt = select(TrackedLink).where(TrackedLink.business_id == ctx.business_id)
    if campaign_id:
        stmt = stmt.where(TrackedLink.campaign_id == campaign_id)
    rows = (await ctx.session.execute(stmt.order_by(TrackedLink.created_at.desc()).limit(200))).scalars().all()
    return {"items": [link_view(link, ctx.services.settings.base_url) for link in rows]}


@router.post("/links", status_code=status.HTTP_201_CREATED)
async def new_link(body: LinkIn, ctx: EditorDep) -> dict[str, Any]:
    link = await create_link(
        ctx.session,
        ctx.business,
        destination=body.destination,
        platform=body.platform,
        campaign_id=body.campaign_id,
        label=body.label,
        utm=body.utm,
    )
    return link_view(link, ctx.services.settings.base_url)


class PageIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    kind: PageKind = "lead_magnet"
    slug: str | None = Field(default=None, max_length=80)
    campaign_id: uuid.UUID | None = None
    content: dict[str, Any] | None = None


class PagePatch(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    settings: dict[str, Any] | None = None


class VariantIn(BaseModel):
    content: dict[str, Any]
    weight: float = Field(default=1.0, ge=0, le=10)


def _public_url(ctx: BusinessDep | EditorDep, page: LandingPage) -> str:
    return f"{ctx.services.settings.base_url}/p/{ctx.business.slug}/{page.slug}"


async def _page(ctx: BusinessDep | EditorDep, page_id: uuid.UUID) -> tuple[LandingPage, list[LandingVariant]]:
    page = await ctx.session.get(LandingPage, page_id)
    if page is None:
        raise NotFound("Page not found")
    variants = list(
        (
            await ctx.session.execute(
                select(LandingVariant).where(LandingVariant.page_id == page.id).order_by(LandingVariant.key)
            )
        ).scalars()
    )
    return page, variants


@router.get("/landing-pages")
async def landing_pages(ctx: BusinessDep) -> dict[str, Any]:
    pages = (
        (
            await ctx.session.execute(
                select(LandingPage)
                .where(LandingPage.business_id == ctx.business_id)
                .order_by(LandingPage.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    out = []
    for page in pages:
        _, variants = await _page(ctx, page.id)
        out.append(page_view(page, variants, _public_url(ctx, page)))
    return {"items": out, "kinds": sorted(TEMPLATES)}


@router.post("/landing-pages", status_code=status.HTTP_201_CREATED)
async def new_landing_page(body: PageIn, ctx: EditorDep) -> dict[str, Any]:
    page = await create_page(
        ctx.session,
        ctx.business,
        title=body.title,
        kind=body.kind,
        slug=body.slug,
        campaign_id=body.campaign_id,
        content=body.content,
    )
    _, variants = await _page(ctx, page.id)
    return page_view(page, variants, _public_url(ctx, page))


@router.get("/landing-pages/{page_id}")
async def get_landing_page(page_id: uuid.UUID, ctx: BusinessDep) -> dict[str, Any]:
    page, variants = await _page(ctx, page_id)
    return page_view(page, variants, _public_url(ctx, page))


@router.patch("/landing-pages/{page_id}")
async def update_landing_page(page_id: uuid.UUID, body: PagePatch, ctx: EditorDep) -> dict[str, Any]:
    page, variants = await _page(ctx, page_id)
    if body.title:
        page.title = body.title
    if body.settings:
        allowed = {"double_opt_in", "consent_text", "stage_on_submit", "fields", "redirect_url"}
        unknown = set(body.settings) - allowed
        if unknown:
            raise ValidationFailed(f"Unknown settings: {sorted(unknown)}")
        page.settings = {**page.settings, **body.settings}
    return page_view(page, variants, _public_url(ctx, page))


@router.put("/landing-pages/{page_id}/variants/{key}")
async def put_variant(page_id: uuid.UUID, key: str, body: VariantIn, ctx: EditorDep) -> dict[str, Any]:
    if not key.isalnum() or len(key) > 8:
        raise ValidationFailed("Variant keys are short letters like A, B, C")
    page, variants = await _page(ctx, page_id)
    existing = next((v for v in variants if v.key == key.upper()), None)
    if existing is None:
        if len(variants) >= 4:
            raise Conflict("Up to four variants per page keeps tests readable")
        existing = LandingVariant(
            business_id=ctx.business_id, page_id=page.id, key=key.upper(), content=body.content, weight=body.weight
        )
        ctx.session.add(existing)
    else:
        existing.content, existing.weight = body.content, body.weight
    await ctx.session.flush()
    _, variants = await _page(ctx, page_id)
    return page_view(page, variants, _public_url(ctx, page))


@router.post("/landing-pages/{page_id}/publish")
async def publish_landing_page(page_id: uuid.UUID, ctx: EditorDep) -> dict[str, Any]:
    page, variants = await _page(ctx, page_id)
    await publish_page(ctx.session, ctx.business, page)
    return page_view(page, variants, _public_url(ctx, page))


@router.post("/landing-pages/{page_id}/unpublish")
async def unpublish_landing_page(page_id: uuid.UUID, ctx: EditorDep) -> dict[str, Any]:
    page, variants = await _page(ctx, page_id)
    page.status = "draft"
    return page_view(page, variants, _public_url(ctx, page))


class PromoIn(BaseModel):
    code: str = Field(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    campaign_id: uuid.UUID | None = None
    description: str | None = Field(default=None, max_length=200)


@router.get("/promo-codes")
async def promo_codes(ctx: BusinessDep) -> dict[str, Any]:
    rows = (
        (
            await ctx.session.execute(
                select(PromoCode).where(PromoCode.business_id == ctx.business_id).order_by(PromoCode.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": str(p.id),
                "code": p.code,
                "uses": p.uses,
                "active": p.active,
                "campaign_id": str(p.campaign_id) if p.campaign_id else None,
                "description": p.description,
            }
            for p in rows
        ]
    }


@router.post("/promo-codes", status_code=status.HTTP_201_CREATED)
async def new_promo_code(body: PromoIn, ctx: EditorDep) -> dict[str, Any]:
    code = body.code.upper()
    exists = await ctx.session.scalar(
        select(PromoCode.id).where(PromoCode.business_id == ctx.business_id, PromoCode.code == code)
    )
    if exists:
        raise Conflict("That code already exists")
    promo = PromoCode(
        business_id=ctx.business_id, code=code, campaign_id=body.campaign_id, description=body.description
    )
    ctx.session.add(promo)
    await ctx.session.flush()
    return {"id": str(promo.id), "code": promo.code, "uses": 0, "active": True}
