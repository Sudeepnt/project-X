"""Discord bot: signed interactions, verified install, /ask through the inbox, /subscribe with consent."""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx
import orjson
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import SecretStr
from sqlalchemy import select, text

from projectx.modules.gtm.models import InboxItem
from projectx.services import Services
from projectx.spine.jobs.worker import Worker
from projectx.spine.twin.models import Contact

KEY = Ed25519PrivateKey.generate()
PUBLIC_HEX = KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
GUILD = "987654321"


@pytest.fixture
def discord(services: Services, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services.settings, "discord_public_key", PUBLIC_HEX)
    monkeypatch.setattr(services.settings, "discord_application_id", "app_1")
    monkeypatch.setattr(services.settings, "discord_client_secret", SecretStr("cs"))
    monkeypatch.setattr(services.settings, "discord_bot_token", SecretStr("bot_tok"))


def _signed(body: dict[str, Any], ts: int | None = None) -> tuple[bytes, dict[str, str]]:
    raw = orjson.dumps(body)
    stamp = str(ts or int(time.time()))
    sig = KEY.sign(stamp.encode() + raw).hex()
    return raw, {"x-signature-ed25519": sig, "x-signature-timestamp": stamp, "content-type": "application/json"}


def _command(name: str, guild: str = GUILD, **options: str) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex,
        "type": 2,
        "guild_id": guild,
        "channel_id": "chan_1",
        "member": {"user": {"id": "u_42", "username": "reader42", "global_name": "Reader"}},
        "data": {"name": name, "options": [{"name": k, "type": 3, "value": v} for k, v in options.items()]},
    }


async def _install(services: Services, client: httpx.AsyncClient, founder: Any, business: dict[str, Any]) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.post("https://discord.com/api/v10/oauth2/token").mock(
            return_value=httpx.Response(200, json={"access_token": "x", "guild": {"id": GUILD, "name": "Readers Club"}})
        )
        url = (await founder.get(f"/api/v1/b/{business['id']}/gtm/discord/install")).json()["url"]
        assert "applications.commands" in url and "state=" in url
        state = httpx.URL(url).params["state"]
        res = await client.get(f"/oauth/discord/bot/callback?code=c&state={state}", follow_redirects=False)
    assert res.status_code == 303 and "connected=discord" in res.headers["location"]


async def test_signatures_and_ping(client: httpx.AsyncClient, discord: None) -> None:
    raw, headers = _signed({"type": 1})
    assert (await client.post("/webhooks/discord/interactions", content=raw, headers=headers)).json() == {"type": 1}
    headers["x-signature-ed25519"] = "00" * 64
    assert (await client.post("/webhooks/discord/interactions", content=raw, headers=headers)).status_code == 401
    stale_raw, stale = _signed({"type": 1}, ts=int(time.time()) - 3600)
    assert (await client.post("/webhooks/discord/interactions", content=stale_raw, headers=stale)).status_code == 401


async def test_install_rejects_forged_state(client: httpx.AsyncClient, discord: None) -> None:
    res = await client.get("/oauth/discord/bot/callback?code=c&state=forged&guild_id=1", follow_redirects=False)
    assert "error=expired" in res.headers["location"]


async def test_ask_goes_through_the_inbox_and_answers_in_channel(
    services: Services, client: httpx.AsyncClient, founder: Any, business: dict[str, Any], discord: None
) -> None:
    await _install(services, client, founder, business)
    unknown_raw, unknown_headers = _signed(_command("ask", guild="nope", question="hi"))
    unknown = await client.post("/webhooks/discord/interactions", content=unknown_raw, headers=unknown_headers)
    assert "isn't connected" in unknown.json()["data"]["content"]

    raw, headers = _signed(_command("ask", question="What time does the newsletter arrive?"))
    res = await client.post("/webhooks/discord/interactions", content=raw, headers=headers)
    assert res.json()["type"] == 4 and "will answer here" in res.json()["data"]["content"]
    bid = uuid.UUID(business["id"])
    async with services.db.tenant(bid) as s:
        item = await s.scalar(select(InboxItem).where(InboxItem.platform == "discord"))
    assert item is not None and item.meta["channel_id"] == "chan_1" and item.author_handle == "reader42"

    with respx.mock(assert_all_called=False) as mock:
        sent = mock.post("https://discord.com/api/v10/channels/chan_1/messages").mock(
            return_value=httpx.Response(200, json={"id": "m_1"})
        )
        for _ in range(3):  # triage → (maybe) auto reply → done
            async with services.db.session() as s:
                await s.execute(text("UPDATE jobs SET run_at = now() WHERE status = 'queued'"))
            await Worker(services).run_until_idle()
        async with services.db.tenant(bid) as s:
            item = await s.get(InboxItem, item.id)
            assert item is not None
            decision_id = item.decision_id
        if item.status == "drafted":  # approve-all mode: the founder approves the drafted answer
            ok = await founder.post(f"/api/v1/b/{business['id']}/decisions/{decision_id}/approve", json={})
            assert ok.status_code == 200, ok.text
            await Worker(services).run_until_idle()
        assert sent.called
        body = orjson.loads(sent.calls[0].request.content)
        assert body["content"].startswith("<@u_42> ")
        assert body["allowed_mentions"] == {"users": ["u_42"]}
        assert sent.calls[0].request.headers["authorization"] == "Bot bot_tok"


async def test_subscribe_records_consent_evidence_and_double_opt_in(
    services: Services, client: httpx.AsyncClient, founder: Any, business: dict[str, Any], discord: None
) -> None:
    await _install(services, client, founder, business)
    bad_raw, bad_headers = _signed(_command("subscribe", email="not-an-email"))
    assert (
        "doesn't look"
        in (await client.post("/webhooks/discord/interactions", content=bad_raw, headers=bad_headers)).json()["data"][
            "content"
        ]
    )
    raw, headers = _signed(_command("subscribe", email="Reader@Example.com"))
    res = await client.post("/webhooks/discord/interactions", content=raw, headers=headers)
    assert "confirm" in res.json()["data"]["content"]
    async with services.db.tenant(uuid.UUID(business["id"])) as s:
        contact = await s.scalar(select(Contact).where(Contact.email == "reader@example.com"))
    assert contact is not None
    assert contact.handles["discord"] == "reader42"
    assert contact.attributes["pending_consent"]["discord_user_id"] == "u_42"  # evidence kept until confirmed
    await Worker(services).run_until_idle()
    assert any(m["to"] == "reader@example.com" and "Confirm" in m["subject"] for m in services.sent_emails)  # type: ignore[attr-defined]

    latest_raw, latest_headers = _signed(_command("latest"))
    latest = await client.post("/webhooks/discord/interactions", content=latest_raw, headers=latest_headers)
    assert latest.json()["data"]["flags"] == 64
