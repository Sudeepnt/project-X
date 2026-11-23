"""WhatsApp Business (Cloud API): one-to-one nurture and replies, never cold broadcasts.

Rules WhatsApp enforces, and we enforce before it does:
  * Free-form text only inside the 24-hour customer-service window (since the person last wrote to us).
  * Outside it, only pre-approved message templates.
  * Marketing needs the person's opt-in. "START"/"SUBSCRIBE" records it (with evidence); "STOP"
    withdraws it immediately.
WhatsApp has no posting API for broadcasts or Channels, so "posts" here stay manual.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.time import utcnow
from projectx.modules.gtm.channels.adapters.base import (
    AccountInfo,
    AdapterContext,
    AdapterError,
    AuthExpired,
    PlatformRejected,
    PublishRequest,
    PublishResult,
)
from projectx.modules.gtm.models import ChannelAccount, Message
from projectx.spine.ledger.service import Actor
from projectx.spine.tenancy.models import Business

GRAPH = "https://graph.facebook.com"
WINDOW = timedelta(hours=24)
OPT_OUT = {"stop", "unsubscribe", "cancel", "stop all", "opt out", "end", "quit"}
OPT_IN = {"start", "subscribe", "yes", "unstop"}
E164 = re.compile(r"^\+?[1-9]\d{6,14}$")


def normalise_phone(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = re.sub(r"[\s().-]", "", raw)
    if not E164.match(digits):
        return None
    return digits if digits.startswith("+") else f"+{digits}"


def _raise(response: Any) -> None:
    if response.status_code < 400:
        return
    try:
        err = response.json().get("error", {})
    except ValueError:
        err = {}
    message = err.get("message") or response.text[:300]
    code = err.get("code")
    if response.status_code in (401, 403) or code == 190:
        raise AuthExpired(f"WhatsApp: {message}")
    if response.status_code == 429 or code in (4, 80007, 130429, 131048, 131056):
        raise AdapterError(f"WhatsApp rate limit: {message}", retry_after=60)
    if code == 131047:  # re-engagement required: outside the 24h window
        raise PlatformRejected("Outside WhatsApp's 24-hour window; an approved template is needed")
    if response.status_code >= 500:
        raise AdapterError(f"WhatsApp is having trouble: {message}")
    raise PlatformRejected(f"WhatsApp refused: {message}")


def _base(ctx: AdapterContext) -> str:
    return f"{GRAPH}/{ctx.settings.get('graph_version', 'v24.0')}"


class WhatsAppAdapter:
    """Connection checks and sending. Publishing a broadcast isn't possible through the API."""

    platform = "whatsapp"

    async def account_info(self, ctx: AdapterContext) -> AccountInfo:
        number_id = ctx.settings.get("phone_number_id")
        if not number_id:
            raise PlatformRejected("Add the WhatsApp phone number ID from Meta's WhatsApp Manager")
        response = await ctx.http.get(
            f"{_base(ctx)}/{number_id}",
            params={"fields": "display_phone_number,verified_name,quality_rating"},
            headers={"Authorization": f"Bearer {ctx.credentials['access_token']}"},
        )
        _raise(response)
        body = response.json()
        return AccountInfo(
            external_id=str(number_id),
            name=body.get("verified_name") or "WhatsApp Business",
            handle=body.get("display_phone_number"),
            capabilities={"quality_rating": body.get("quality_rating")},
        )

    async def send(
        self, ctx: AdapterContext, *, to: str, text: str | None = None, template: dict[str, Any] | None = None
    ) -> str:
        number_id = ctx.settings.get("phone_number_id") or ctx.account_external_id
        payload: dict[str, Any] = {"messaging_product": "whatsapp", "to": to.lstrip("+")}
        if template:
            payload |= {
                "type": "template",
                "template": {
                    "name": template["name"],
                    "language": {"code": template.get("language", "en")},
                    "components": [
                        {
                            "type": "body",
                            "parameters": [{"type": "text", "text": str(p)[:1000]} for p in template.get("params", [])],
                        }
                    ]
                    if template.get("params")
                    else [],
                },
            }
        else:
            payload |= {"type": "text", "text": {"body": (text or "")[:4096], "preview_url": True}}
        response = await ctx.http.post(
            f"{_base(ctx)}/{number_id}/messages",
            json=payload,
            headers={"Authorization": f"Bearer {ctx.credentials['access_token']}"},
        )
        _raise(response)
        return str(response.json()["messages"][0]["id"])

    async def publish(self, ctx: AdapterContext, req: PublishRequest) -> PublishResult:
        raise PlatformRejected("WhatsApp broadcasts and Channels have no posting API; post this one yourself")

    async def verify(self, ctx: AdapterContext, external_id: str) -> bool:
        return True

    async def delete(self, ctx: AdapterContext, external_id: str) -> None:
        return None

    async def metrics(self, ctx: AdapterContext, external_ids: list[str]) -> dict[str, dict[str, float]]:
        return {}


def in_window(contact_attributes: dict[str, Any] | None, now: datetime | None = None) -> bool:
    raw = (contact_attributes or {}).get("whatsapp_last_inbound_at")
    if not raw:
        return False
    try:
        last = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return (now or utcnow()) - last < WINDOW


async def handle_webhook_value(
    session: AsyncSession, business: Business, account_id: uuid.UUID, value: dict[str, Any]
) -> int:
    """One `messages` change from the WhatsApp webhook: inbound messages and delivery statuses."""
    from projectx.modules.gtm.crm.lifecycle import Cause, ConsentInput, grant_consent, revoke_consent, upsert_contact
    from projectx.modules.gtm.inbox.service import ingest

    handled = 0
    names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts", [])}
    for msg in value.get("messages", []):
        phone = normalise_phone(msg.get("from"))
        if phone is None:
            continue
        text = (msg.get("text") or {}).get("body") or (msg.get("button") or {}).get("text") or ""
        if not text and msg.get("type") not in ("text", "button"):
            text = f"[{msg.get('type', 'message')}]"
        contact, _ = await upsert_contact(
            session,
            business,
            source="whatsapp",
            cause=Cause("whatsapp", actor=Actor.public()),
            phone=phone,
            name=names.get(msg.get("from")),
        )
        contact.handles = {**(contact.handles or {}), "whatsapp": phone}
        contact.attributes = {**(contact.attributes or {}), "whatsapp_last_inbound_at": utcnow().isoformat()}
        keyword = text.strip().lower()
        evidence = {
            "source": "whatsapp",
            "message_id": msg.get("id"),
            "keyword": keyword,
            "phone_number_id": value.get("metadata", {}).get("phone_number_id"),
        }
        if keyword in OPT_OUT:
            await revoke_consent(session, contact, channel="whatsapp", source="whatsapp_keyword", actor=Actor.public())
            handled += 1
            continue
        if keyword in OPT_IN:
            await grant_consent(
                session,
                contact,
                ConsentInput(channel="whatsapp", purpose="marketing", source="whatsapp_keyword", evidence=evidence),
                actor=Actor.public(),
            )
            handled += 1
            continue
        item = await ingest(
            session,
            business,
            platform="whatsapp",
            kind="dm",
            external_id=str(msg.get("id")),
            text=text,
            author_handle=phone,
            author_name=names.get(msg.get("from")),
            account_id=account_id,
            thread_id=phone,
        )
        handled += int(item is not None)
    for status in value.get("statuses", []):
        message = await session.scalar(select(Message).where(Message.provider_id == status.get("id")))
        if message is None:
            continue
        state = status.get("status")
        if state == "read" and message.opened_at is None:
            message.opened_at = utcnow()
        elif state == "failed":
            message.status = "failed"
            errors = status.get("errors") or [{}]
            message.error = str(errors[0].get("title") or errors[0].get("message") or "failed")[:500]
        handled += 1
    return handled


async def account_for(session: AsyncSession, business_id: uuid.UUID) -> ChannelAccount | None:
    account: ChannelAccount | None = await session.scalar(
        select(ChannelAccount)
        .where(
            ChannelAccount.business_id == business_id,
            ChannelAccount.platform == "whatsapp",
            ChannelAccount.status.in_(("connected", "practice")),
        )
        .order_by(ChannelAccount.created_at)
    )
    return account
