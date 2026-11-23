"""WhatsApp: connect, signed webhook (inbox, STOP/START consent, receipts), 24-hour window vs templates."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import timedelta
from typing import Any

import httpx
import orjson
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import select

from projectx.kernel.time import utcnow
from projectx.modules.gtm.channels.whatsapp import in_window, normalise_phone
from projectx.modules.gtm.crm.lifecycle import has_consent
from projectx.modules.gtm.models import InboxItem, Message
from projectx.services import Services
from projectx.spine.jobs.queue import enqueue
from projectx.spine.jobs.worker import Worker
from projectx.spine.twin.models import Contact

GRAPH = "https://graph.facebook.com/v24.0"
NUMBER_ID = "1098765"  # each test connects its own number (one number = one business)
APP_SECRET = "meta-app-secret"


@pytest.fixture
def meta(services: Services, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services.settings, "meta_app_secret", SecretStr(APP_SECRET))


def _signed(payload: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
    raw = orjson.dumps(payload)
    sig = "sha256=" + hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"x-hub-signature-256": sig, "content-type": "application/json"}


def _inbound(
    text: str, msg_id: str | None = None, phone: str = "919876543210", number_id: str = NUMBER_ID
) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": number_id},
                            "contacts": [{"wa_id": phone, "profile": {"name": "Asha"}}],
                            "messages": [
                                {
                                    "from": phone,
                                    "id": msg_id or f"wamid.{uuid.uuid4().hex}",
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


async def _connect(founder: Any, business: dict[str, Any], number_id: str = NUMBER_ID) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{GRAPH}/{number_id}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "display_phone_number": "+91 98765 00000",
                    "verified_name": "Morning Ledger",
                    "quality_rating": "GREEN",
                },
            )
        )
        res = await founder.post(
            f"/api/v1/b/{business['id']}/gtm/channels/token",
            json={
                "platform": "whatsapp",
                "credentials": {"access_token": "EAAG"},
                "settings": {"phone_number_id": number_id},
            },
        )
    assert res.status_code == 201, res.text


def test_phone_and_window_helpers() -> None:
    assert normalise_phone("+91 98765-43210") == "+919876543210"
    assert normalise_phone("919876543210") == "+919876543210"
    assert normalise_phone("call me") is None
    assert in_window({"whatsapp_last_inbound_at": utcnow().isoformat()})
    assert not in_window({"whatsapp_last_inbound_at": (utcnow() - timedelta(hours=25)).isoformat()})
    assert not in_window({})


async def test_connect_requires_a_number_id(founder: Any, business: dict[str, Any]) -> None:
    res = await founder.post(
        f"/api/v1/b/{business['id']}/gtm/channels/token",
        json={"platform": "whatsapp", "credentials": {"access_token": "x"}, "settings": {}},
    )
    assert res.status_code == 422


async def test_webhook_inbox_consent_keywords_and_receipts(
    services: Services, client: httpx.AsyncClient, founder: Any, business: dict[str, Any], meta: None
) -> None:
    await _connect(founder, business)
    bid = uuid.UUID(business["id"])
    raw, headers = _signed(_inbound("Hi! Do you have a student discount?"))
    bad = dict(headers, **{"x-hub-signature-256": "sha256=00"})
    assert (await client.post("/webhooks/meta", content=raw, headers=bad)).status_code == 401
    assert (await client.post("/webhooks/meta", content=raw, headers=headers)).json()["handled"] == 1
    async with services.db.tenant(bid) as s:
        item = await s.scalar(select(InboxItem).where(InboxItem.platform == "whatsapp"))
        contact = await s.scalar(select(Contact).where(Contact.phone == "+919876543210"))
        assert item is not None and item.thread_id == "+919876543210" and item.kind == "dm"
        assert contact is not None and in_window(contact.attributes)
        assert not await has_consent(s, contact, "whatsapp"), "writing to us isn't marketing consent"

    for keyword, expected in (("START", True), ("stop", False)):
        raw, headers = _signed(_inbound(keyword))
        await client.post("/webhooks/meta", content=raw, headers=headers)
        async with services.db.tenant(bid) as s:
            contact = await s.scalar(select(Contact).where(Contact.phone == "+919876543210"))
            assert contact is not None
            assert await has_consent(s, contact, "whatsapp") is expected, keyword


async def test_sending_respects_the_24_hour_window(
    services: Services, client: httpx.AsyncClient, founder: Any, business: dict[str, Any], meta: None
) -> None:
    await _connect(founder, business, "2098765")
    bid = uuid.UUID(business["id"])
    raw, headers = _signed(_inbound("START", number_id="2098765"))
    await client.post("/webhooks/meta", content=raw, headers=headers)
    async with services.db.tenant(bid) as s:
        contact = await s.scalar(select(Contact).where(Contact.phone == "+919876543210"))
        assert contact is not None
        inside = Message(
            business_id=bid, contact_id=contact.id, channel="whatsapp", kind="sequence", body_text="Welcome!"
        )
        s.add(inside)
        await s.flush()
        await enqueue(s, "gtm.send_message", {"message_id": str(inside.id)}, business_id=bid)
    with respx.mock(assert_all_called=False) as mock:
        sent = mock.post(f"{GRAPH}/2098765/messages").mock(
            return_value=httpx.Response(200, json={"messages": [{"id": "wamid.out1"}]})
        )
        await Worker(services).run_until_idle()
        body = orjson.loads(sent.calls[0].request.content)
        assert body["type"] == "text" and body["to"] == "919876543210"

        async with services.db.tenant(bid) as s:  # 25 hours later: text isn't allowed; a template is
            contact = await s.scalar(select(Contact).where(Contact.phone == "+919876543210"))
            assert contact is not None
            contact.attributes = {
                **contact.attributes,
                "whatsapp_last_inbound_at": (utcnow() - timedelta(hours=25)).isoformat(),
            }
            plain = Message(business_id=bid, contact_id=contact.id, channel="whatsapp", kind="sequence", body_text="Hi")
            templated = Message(
                business_id=bid,
                contact_id=contact.id,
                channel="whatsapp",
                kind="sequence",
                body_text="Hi",
                meta={"template": {"name": "weekly_digest", "language": "en", "params": ["Asha"]}},
            )
            s.add_all([plain, templated])
            await s.flush()
            for m in (plain, templated):
                await enqueue(s, "gtm.send_message", {"message_id": str(m.id)}, business_id=bid)
            plain_id, templated_id = plain.id, templated.id
        await Worker(services).run_until_idle()
        template_call = orjson.loads(sent.calls[-1].request.content)
        assert template_call["type"] == "template" and template_call["template"]["name"] == "weekly_digest"
    async with services.db.tenant(bid) as s:
        plain_msg = await s.get(Message, plain_id)
        templated_msg = await s.get(Message, templated_id)
        assert plain_msg is not None and plain_msg.status == "suppressed" and "template" in (plain_msg.error or "")
        assert templated_msg is not None and templated_msg.status == "sent"

    # A delivery failure receipt updates the message.
    receipt = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "2098765"},
                            "statuses": [
                                {"id": "wamid.out1", "status": "failed", "errors": [{"title": "Message undeliverable"}]}
                            ],
                        },
                    }
                ],
            }
        ],
    }
    raw, headers = _signed(receipt)
    await client.post("/webhooks/meta", content=raw, headers=headers)
    async with services.db.tenant(bid) as s:
        first = await s.scalar(select(Message).where(Message.provider_id == "wamid.out1"))
        assert first is not None and first.status == "failed" and first.error == "Message undeliverable"


async def test_one_account_belongs_to_one_business(founder: Any, business: dict[str, Any]) -> None:
    from tests.conftest import new_business

    await _connect(founder, business, "3098765")
    other = await new_business(founder)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{GRAPH}/3098765").mock(return_value=httpx.Response(200, json={"verified_name": "X"}))
        body = {
            "platform": "whatsapp",
            "credentials": {"access_token": "t"},
            "settings": {"phone_number_id": "3098765"},
        }
        taken = await founder.post(f"/api/v1/b/{other['id']}/gtm/channels/token", json=body)
        assert taken.status_code == 409
        channels = (await founder.get(f"/api/v1/b/{business['id']}/gtm/channels")).json()["platforms"]
        account = next(a for p in channels if p["key"] == "whatsapp" for a in p["accounts"])
        assert (
            await founder.delete(f"/api/v1/b/{business['id']}/gtm/channels/accounts/{account['id']}")
        ).status_code in (200, 204)
        moved = await founder.post(f"/api/v1/b/{other['id']}/gtm/channels/token", json=body)
        assert moved.status_code == 201, "once disconnected, another business can take it over"
