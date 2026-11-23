"""Billing endpoints: see the plan, check out, change or cancel, open the customer portal, and the webhook."""

from __future__ import annotations

from typing import Any, Literal

import orjson
import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from projectx.api.deps import BusinessDep, OwnerDep
from projectx.kernel.errors import Conflict
from projectx.modules.gtm.crm.webhooks import verify_standard_webhook
from projectx.services import Services
from projectx.spine.billing import service as billing

router = APIRouter(prefix="/api/v1/b/{business_id}/billing", tags=["billing"])
public_router = APIRouter(include_in_schema=False)
log = structlog.get_logger("billing")


@router.get("")
async def overview(ctx: BusinessDep) -> dict[str, Any]:
    sub = await billing.subscription_of(ctx.session, ctx.business_id)
    return billing.billing_view(ctx.business, sub, ctx.services.settings)


class CheckoutIn(BaseModel):
    plan: Literal["autopilot", "growth", "scale"]


@router.post("/checkout")
async def checkout(body: CheckoutIn, ctx: OwnerDep) -> dict[str, Any]:
    return await billing.start_checkout(
        ctx.session, ctx.services, ctx.business, plan=body.plan, email=ctx.user.email, name=ctx.user.name
    )


@router.post("/portal")
async def portal(ctx: OwnerDep) -> dict[str, str]:
    sub = await billing.subscription_of(ctx.session, ctx.business_id)
    if not sub.provider_customer_id:
        raise Conflict("There's no billing account yet; choose a plan first")
    return {"url": await billing.client(ctx.services).portal(sub.provider_customer_id)}


class CancelIn(BaseModel):
    undo: bool = False


@router.post("/cancel")
async def cancel(body: CancelIn, ctx: OwnerDep) -> dict[str, Any]:
    sub = await billing.cancel(ctx.session, ctx.services, ctx.business, undo=body.undo)
    return billing.billing_view(ctx.business, sub, ctx.services.settings)


@public_router.post("/webhooks/billing/dodo")
async def dodo_webhook(request: Request) -> JSONResponse:
    services: Services = request.app.state.services
    secret = services.settings.dodo_webhook_secret
    if secret is None:
        return JSONResponse({"error": "not configured"}, status_code=404)
    payload = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    if not verify_standard_webhook(payload, headers, secret.get_secret_value()):
        return JSONResponse({"error": "bad signature"}, status_code=401)
    try:
        event = orjson.loads(payload)
    except orjson.JSONDecodeError:
        return JSONResponse({"error": "bad payload"}, status_code=400)
    event_id = headers["webhook-id"]
    async with services.db.session() as session:
        business_id = await billing.resolve_business(session, event)
        if business_id is None:
            # Acknowledge (so the provider stops retrying) but remember it for investigation.
            await billing.record_once(session, event_id, "dodo", str(event.get("type", "")))
            log.warning("billing.unknown_business", event_id=event_id, type=event.get("type"))
            return JSONResponse({"ok": True, "outcome": "unknown_business"})
    async with services.db.tenant(business_id) as session:
        outcome = await billing.apply_dodo_event(session, services.settings, event_id, event, business_id)
    log.info("billing.webhook", event_id=event_id, outcome=outcome)
    return JSONResponse({"ok": True, "outcome": outcome})
