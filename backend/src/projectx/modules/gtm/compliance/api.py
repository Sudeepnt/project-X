"""Compliance center endpoints, Meta's data-deletion callback, legal pages and the media kit."""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, EmailStr
from sqlalchemy import select

from projectx.api.deps import AdminDep, BusinessDep
from projectx.kernel.errors import ValidationFailed
from projectx.kernel.time import utcnow
from projectx.modules.gtm.analytics.service import channel_table
from projectx.modules.gtm.compliance.service import create_request, parse_signed_request, request_view
from projectx.modules.gtm.config import gtm_settings, update_gtm_settings
from projectx.modules.gtm.cycles import numbers
from projectx.modules.gtm.models import BrandBook, ChannelAccount, DataRequest
from projectx.modules.gtm.nurture.newsletter import subscribers
from projectx.services import Services
from projectx.spine.ledger.service import Actor
from projectx.spine.tenancy.models import Business

router = APIRouter(prefix="/api/v1/b/{business_id}/gtm", tags=["gtm: compliance & sales"])
public_router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[3] / "templates"))


class DataRequestIn(BaseModel):
    kind: Literal["export", "erase"]
    contact_id: uuid.UUID | None = None
    email: EmailStr | None = None


@router.get("/compliance/requests")
async def requests(ctx: BusinessDep) -> list[dict[str, Any]]:
    rows = (
        (
            await ctx.session.execute(
                select(DataRequest)
                .where(DataRequest.business_id == ctx.business_id)
                .order_by(DataRequest.requested_at.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    return [request_view(r) for r in rows]


@router.post("/compliance/requests", status_code=status.HTTP_201_CREATED)
async def new_request(body: DataRequestIn, ctx: AdminDep) -> dict[str, Any]:
    if body.contact_id is None and body.email is None:
        raise ValidationFailed("Say whose data: a contact or an email address")
    request = await create_request(
        ctx.session,
        ctx.services,
        ctx.business_id,
        kind=body.kind,
        contact_id=body.contact_id,
        email=str(body.email) if body.email else None,
        source="cockpit",
        actor=Actor.user(ctx.user.id),
    )
    return request_view(request)


@router.get("/compliance/overview")
async def compliance_overview(ctx: BusinessDep) -> dict[str, Any]:
    s = gtm_settings(ctx.business)
    audience = await subscribers(ctx.session, ctx.business_id)
    return {
        "opted_in_subscribers": len(audience),
        "ai_label": s["ai_label"],
        "double_opt_in": s["newsletter"].get("double_opt_in", True),
        "postal_address_set": bool((ctx.business.settings or {}).get("postal_address")),
        "legal_pages": {
            k: f"{ctx.services.settings.base_url}/legal/{ctx.business.slug}/{k}"
            for k in ("privacy", "terms", "data-deletion")
        },
        "data_deletion_callback": f"{ctx.services.settings.base_url}/webhooks/meta/data-deletion",
    }


class LabelIn(BaseModel):
    ai_label: Literal["platform", "text", "off"]


@router.put("/compliance/ai-label")
async def set_ai_label(body: LabelIn, ctx: AdminDep) -> dict[str, Any]:
    update_gtm_settings(ctx.business, {"ai_label": body.ai_label})
    return {"ai_label": body.ai_label}


@public_router.post("/webhooks/meta/data-deletion")
async def meta_data_deletion(request: Request) -> Response:
    services: Services = request.app.state.services
    secret = services.settings.meta_app_secret
    form = await request.form()
    data = parse_signed_request(str(form.get("signed_request", "")), secret.get_secret_value()) if secret else None
    if data is None:
        return JSONResponse({"error": "invalid signed_request"}, status_code=400)
    user_id = str(data.get("user_id", ""))
    code = f"meta-{uuid.uuid4().hex[:12]}"
    erased = 0
    async with services.db.session() as session:
        business_ids = (await session.execute(select(Business.id).where(Business.status == "active"))).scalars().all()
    from projectx.spine.twin.models import Contact

    for business_id in business_ids:
        async with services.db.tenant(business_id) as session:
            for platform in ("facebook", "instagram"):
                contacts = (
                    (
                        await session.execute(
                            select(Contact).where(
                                Contact.business_id == business_id, Contact.handles[platform].astext == user_id.lower()
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for contact in contacts:
                    await create_request(
                        session,
                        services,
                        business_id,
                        kind="erase",
                        contact_id=contact.id,
                        email=None,
                        source="platform_callback",
                        actor=Actor.integration("meta"),
                    )
                    erased += 1
    return JSONResponse({"url": f"{services.settings.base_url}/privacy/deletion/{code}", "confirmation_code": code})


@public_router.get("/privacy/deletion/{code}")
async def deletion_status(code: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><title>Deletion request</title><main style='font-family:system-ui;"
        f"max-width:560px;margin:15vh auto'><h1>Deletion request received</h1><p>Reference "
        f"{code[:40]}. Personal data linked to this account has been erased.</p></main>"
    )


@public_router.get("/legal/{business_slug}/{page}")
async def legal_page(business_slug: str, page: str, request: Request) -> Response:
    if page not in ("privacy", "terms", "data-deletion"):
        return HTMLResponse("<h1>Not found</h1>", status_code=404)
    services: Services = request.app.state.services
    async with services.db.session() as session:
        business = await session.scalar(select(Business).where(Business.slug == business_slug))
    if business is None:
        return HTMLResponse("<h1>Not found</h1>", status_code=404)
    contact = (business.settings or {}).get("reply_to_email") or ""
    return templates.TemplateResponse(
        request,
        f"public/legal_{page.replace('-', '_')}.html",
        {"business": business, "contact": contact, "primary": "#0071e3", "updated": utcnow().strftime("%d %B %Y")},
    )


# ------------------------------------------------------------------ media kit (sales materials)


async def media_kit_data(services: Services, business: Business) -> dict[str, Any]:
    async with services.db.tenant(business.id) as session:
        n = await numbers(session, business.id)
        accounts = (
            (
                await session.execute(
                    select(ChannelAccount).where(
                        ChannelAccount.business_id == business.id, ChannelAccount.status.in_(("connected", "practice"))
                    )
                )
            )
            .scalars()
            .all()
        )
        channels = await channel_table(session, business.id, utcnow() - timedelta(days=30))
        audience = await subscribers(session, business.id)
        book = await session.scalar(
            select(BrandBook)
            .where(BrandBook.business_id == business.id, BrandBook.status == "approved")
            .order_by(BrandBook.version.desc())
            .limit(1)
        )
    reach = sum(c["reach"] for c in channels)
    engagements = sum(c["engagements"] for c in channels)
    return {
        "business": business.name,
        "summary": book.summary if book else business.description,
        "audience": book.audience_summary if book else "",
        "channels": [{"platform": a.platform, "name": a.name, "followers": a.followers} for a in accounts],
        "newsletter_subscribers": len(audience),
        "monthly_reach": reach,
        "engagement_rate": round(engagements / reach, 4) if reach else None,
        "customers": n["customers"],
        "as_of": utcnow().date().isoformat(),
    }


@router.get("/sales/media-kit")
async def media_kit(ctx: BusinessDep) -> dict[str, Any]:
    data = await media_kit_data(ctx.services, ctx.business)
    enabled = bool(gtm_settings(ctx.business).get("media_kit_public"))
    return {
        **data,
        "public": enabled,
        "public_url": f"{ctx.services.settings.base_url}/p/{ctx.business.slug}/media-kit" if enabled else None,
    }


class MediaKitIn(BaseModel):
    public: bool


@router.put("/sales/media-kit")
async def set_media_kit(body: MediaKitIn, ctx: AdminDep) -> dict[str, Any]:
    update_gtm_settings(ctx.business, {"media_kit_public": body.public})
    return {"public": body.public}


@public_router.get("/p/{business_slug}/media-kit")
async def public_media_kit(business_slug: str, request: Request) -> Response:
    services: Services = request.app.state.services
    async with services.db.session() as session:
        business = await session.scalar(
            select(Business).where(Business.slug == business_slug, Business.status == "active")
        )
    if business is None or not gtm_settings(business).get("media_kit_public"):
        return HTMLResponse("<h1>Not found</h1>", status_code=404)
    data = await media_kit_data(services, business)
    return templates.TemplateResponse(
        request, "public/media_kit.html", {"business": business, "kit": data, "primary": "#0071e3"}
    )
