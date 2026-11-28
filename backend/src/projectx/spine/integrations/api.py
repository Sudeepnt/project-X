"""Connect tools: see them all, add your own keys, use ours, test, or turn off."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from projectx.api.deps import BusinessDep, OwnerDep
from projectx.kernel.errors import ValidationFailed
from projectx.spine.integrations import service as integrations
from projectx.spine.ledger.service import Actor

router = APIRouter(prefix="/api/v1/b/{business_id}/integrations", tags=["integrations"])


class KeysIn(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


def _with_urls(ctx: BusinessDep, item: dict[str, Any]) -> dict[str, Any]:
    path = item.get("webhook_path")
    if path:
        item["webhook_url"] = ctx.services.settings.base_url + path.format(slug=ctx.business.slug)
    return item


@router.get("")
async def list_tools(ctx: BusinessDep) -> dict[str, Any]:
    return {"items": [_with_urls(ctx, i) for i in await integrations.overview(ctx.session, ctx.services)]}


@router.put("/{tool}")
async def own_keys(tool: str, body: KeysIn, ctx: OwnerDep) -> dict[str, Any]:
    """Use your own keys. They're tested now, sealed in the vault, and never shown again."""
    await integrations.save_own(
        ctx.session, ctx.services, ctx.business_id, tool, body.values, actor=Actor.user(ctx.user.id)
    )
    return next(_with_urls(ctx, i) for i in await integrations.overview(ctx.session, ctx.services) if i["tool"] == tool)


@router.post("/{tool}/platform")
async def use_ours(tool: str, ctx: OwnerDep) -> dict[str, Any]:
    """Use the platform's account. For Stripe this starts Connect onboarding and returns the link to finish it."""
    actor = Actor.user(ctx.user.id)
    if tool == "stripe":
        from projectx.modules.finance.payments import register_connect_account
        from projectx.spine.integrations.stripe import start_connect

        integrations.tool_or_404(tool)
        if not integrations.tool_or_404(tool).platform(ctx.services.settings):
            raise ValidationFailed("The platform's Stripe isn't set up yet; add your own keys instead.")
        account, link = await start_connect(
            ctx.services, ctx.business, return_url=f"{ctx.services.settings.web_url}/settings/integrations?stripe=done"
        )
        await integrations.use_platform(
            ctx.session,
            ctx.services,
            ctx.business_id,
            tool,
            actor=actor,
            status="pending",
            settings={"account_id": account},
        )
        await register_connect_account(ctx.session, ctx.business_id, account)
        return {"tool": tool, "mode": "platform", "status": "pending", "onboarding_url": link}
    row = await integrations.use_platform(ctx.session, ctx.services, ctx.business_id, tool, actor=actor)
    return {"tool": tool, "mode": row.mode, "status": row.status}


@router.post("/stripe/refresh")
async def stripe_refresh(ctx: OwnerDep) -> dict[str, Any]:
    """After Stripe onboarding: check the connected account can take payments."""
    from projectx.spine.integrations.stripe import connect_ready

    row = await integrations._row(ctx.session, "stripe", lock=True)
    if row is None or row.mode != "platform" or not row.settings.get("account_id"):
        raise ValidationFailed("Stripe isn't connected through the platform")
    row.status = "ok" if await connect_ready(ctx.services, row.settings["account_id"]) else "pending"
    return {"tool": "stripe", "mode": row.mode, "status": row.status}


@router.delete("/{tool}", status_code=204)
async def turn_off(tool: str, ctx: OwnerDep) -> None:
    await integrations.turn_off(ctx.session, ctx.services, ctx.business_id, tool, actor=Actor.user(ctx.user.id))
    ctx.services.gateway.forget_route(ctx.business_id)
