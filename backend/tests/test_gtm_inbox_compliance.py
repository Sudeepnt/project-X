"""Inbox triage rules and privacy obligations (export, erasure, Meta's data-deletion callback)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from typing import Any

import orjson
from sqlalchemy import select, text

from projectx.modules.gtm.compliance.service import parse_signed_request
from projectx.modules.gtm.inbox.service import rule_triage
from projectx.modules.gtm.models import InboxItem
from projectx.services import Services
from projectx.spine.decisions.models import Decision
from projectx.spine.jobs.worker import Worker
from projectx.spine.ledger.models import LedgerEvent
from projectx.spine.twin.models import Contact


def gtm(business: dict[str, Any], path: str) -> str:
    return f"/api/v1/b/{business['id']}/gtm{path}"


def test_rule_triage() -> None:
    assert rule_triage("FREE FOLLOWERS dm me for crypto")[0] == "spam"  # type: ignore[index]
    assert rule_triage("This is a scam and I want a refund")[0] == "negative"  # type: ignore[index]
    assert rule_triage("I'm going to take legal action")[0] == "sensitive"  # type: ignore[index]
    assert rule_triage("When is the next issue?") is None


async def test_inbox_triage_escalates_and_drafts(services: Services, founder: Any, business: dict[str, Any]) -> None:
    bid = uuid.UUID(business["id"])
    await founder.post(gtm(business, "/channels/practice"), json={"platform": "instagram", "followers": 1000})
    res = await founder.post(gtm(business, "/inbox/simulate"), json={})
    assert res.status_code == 201 and res.json()["created"] == 4
    await Worker(services).run_until_idle()
    inbox = (await founder.get(gtm(business, "/inbox"))).json()
    by_category = {i["category"]: i for i in inbox["items"]}
    if "negative" in by_category:
        assert by_category["negative"]["status"] == "escalated"
    if "spam" in by_category:
        assert by_category["spam"]["status"] == "closed"
    assert all(i["contact_id"] for i in inbox["items"] if i["kind"] in ("comment", "dm"))
    async with services.db.tenant(bid) as s:
        cards = (
            (
                await s.execute(
                    select(Decision).where(
                        Decision.business_id == bid, Decision.action_type.in_(("gtm.reply_public", "gtm.reply_private"))
                    )
                )
            )
            .scalars()
            .all()
        )
    assert all(c.status == "pending" for c in cards)  # replies wait: nothing earned yet
    target = next((i for i in inbox["items"] if i["status"] == "drafted"), None)
    if target:
        reply = await founder.post(gtm(business, f"/inbox/{target['id']}/reply"), json={"text": "Thanks for asking!"})
        assert reply.status_code == 200
        await Worker(services).run_until_idle()
        async with services.db.tenant(bid) as s:
            item = await s.get(InboxItem, uuid.UUID(target["id"]))
        assert item is not None and item.status == "replied"


async def test_export_and_erase(services: Services, founder: Any, business: dict[str, Any]) -> None:
    bid = uuid.UUID(business["id"])
    email = f"gdpr-{uuid.uuid4().hex[:6]}@example.com"
    contact = (
        await founder.post(gtm(business, "/contacts"), json={"email": email, "name": "Erin Erase", "stage": "lead"})
    ).json()
    export = (await founder.post(gtm(business, "/compliance/requests"), json={"kind": "export", "email": email})).json()
    assert export["status"] == "completed" and "download_url" in export["result"]
    erase = (
        await founder.post(gtm(business, "/compliance/requests"), json={"kind": "erase", "contact_id": contact["id"]})
    ).json()
    assert erase["status"] == "completed"
    async with services.db.tenant(bid) as s:
        row = await s.get(Contact, uuid.UUID(contact["id"]))
        events = (await s.execute(select(LedgerEvent.data).where(LedgerEvent.business_id == bid))).scalars().all()
    assert row is not None and row.email is None and row.name is None and row.erased_at is not None
    assert all(email not in orjson.dumps(e).decode() for e in events)  # the ledger never held it
    listed = (await founder.get(gtm(business, "/contacts?q=Erin"))).json()
    assert listed["total"] == 0
    async with services.db.session() as s:  # nothing leaked into job payloads either
        leftovers = (
            await s.execute(text("select count(*) from jobs where payload::text like :e"), {"e": f"%{email}%"})
        ).scalar()
    assert leftovers == 0


def test_meta_signed_request() -> None:
    payload = (
        base64.urlsafe_b64encode(orjson.dumps({"algorithm": "HMAC-SHA256", "user_id": "123"})).decode().rstrip("=")
    )
    sig = base64.urlsafe_b64encode(hmac.new(b"secret", payload.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    assert parse_signed_request(f"{sig}.{payload}", "secret") == {"algorithm": "HMAC-SHA256", "user_id": "123"}
    assert parse_signed_request(f"{sig}.{payload}", "wrong") is None
    assert parse_signed_request("garbage", "secret") is None
