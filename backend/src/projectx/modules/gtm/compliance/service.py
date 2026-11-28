"""Privacy obligations: export or erase a person's data, honour platform data-deletion callbacks.

Erasure removes personal data from every mutable table (contact fields, message bodies, inbox
text, consents' evidence). The ledger never held personal data, so it stays intact and still
proves what happened, without saying to whom.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from typing import Any

import orjson
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.errors import NotFound
from projectx.kernel.ids import short_code
from projectx.kernel.time import utcnow
from projectx.modules.gtm.models import DataRequest, InboxItem, Message, SequenceEnrollment
from projectx.services import Services
from projectx.spine.calendar.models import CalendarEvent
from projectx.spine.ledger.service import Actor, append
from projectx.spine.twin.models import Consent, Contact, ContactStageChange, Deal, Payment, Touch


def email_hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


async def _events_with(session: AsyncSession, contact: Contact) -> list[CalendarEvent]:
    """Calendar events this person is linked to or invited to (attendees hold their email)."""
    conditions = [CalendarEvent.contact_id == contact.id]
    if contact.email:
        conditions.append(CalendarEvent.attendees.contains([{"email": contact.email.strip().lower()}]))
    stmt = select(CalendarEvent).where(CalendarEvent.business_id == contact.business_id, or_(*conditions))
    return list((await session.execute(stmt)).scalars())


async def export_contact(session: AsyncSession, contact: Contact) -> dict[str, Any]:
    bid, cid = contact.business_id, contact.id

    async def rows(model: Any, *filters: Any) -> list[dict[str, Any]]:
        out = []
        for row in (await session.execute(select(model).where(model.business_id == bid, *filters))).scalars():
            out.append(
                {c.name: getattr(row, c.name) for c in model.__table__.columns if c.name not in ("business_id",)}
            )
        return out

    exported: dict[str, Any] = orjson.loads(
        orjson.dumps(
            {
                "contact": {
                    c.name: getattr(contact, c.name) for c in Contact.__table__.columns if c.name != "business_id"
                },
                "consents": await rows(Consent, Consent.contact_id == cid),
                "touches": await rows(Touch, Touch.contact_id == cid),
                "stage_history": await rows(ContactStageChange, ContactStageChange.contact_id == cid),
                "messages": await rows(Message, Message.contact_id == cid),
                "inbox": await rows(InboxItem, InboxItem.contact_id == cid),
                "deals": await rows(Deal, Deal.contact_id == cid),
                "payments": await rows(Payment, Payment.contact_id == cid),
                "calendar_events": [
                    {"id": e.id, "title": e.title, "starts_at": e.starts_at, "ends_at": e.ends_at, "status": e.status}
                    for e in await _events_with(session, contact)
                ],
                "exported_at": utcnow(),
            },
            default=str,
        )
    )
    return exported


async def erase_contact(session: AsyncSession, contact: Contact, *, actor: Actor) -> None:
    bid, cid = contact.business_id, contact.id
    for event in await _events_with(session, contact):
        email = (contact.email or "").strip().lower()
        event.attendees = [
            {"email": "erased@invalid", "name": None} if a.get("email") == email else a for a in event.attendees
        ]
        if event.contact_id == cid:
            event.contact_id = None
    contact.email = contact.phone = contact.name = contact.company = contact.job_title = None
    contact.handles, contact.attributes, contact.tags = {}, {}, []
    contact.first_touch = contact.last_touch = None
    contact.next_action = contact.status_reason = None
    contact.side_state, contact.erased_at = "do_not_contact", utcnow()
    contact.unsubscribed_at = contact.unsubscribed_at or utcnow()
    await session.execute(
        update(Message)
        .where(Message.business_id == bid, Message.contact_id == cid)
        .values(subject=None, body_text="[erased]", body_html=None)
    )
    await session.execute(
        update(InboxItem)
        .where(InboxItem.business_id == bid, InboxItem.contact_id == cid)
        .values(text="[erased]", author_handle=None, author_name=None, ai_draft=None, reply_text=None)
    )
    await session.execute(
        update(Consent).where(Consent.business_id == bid, Consent.contact_id == cid).values(evidence={"erased": True})
    )
    await session.execute(update(Touch).where(Touch.business_id == bid, Touch.contact_id == cid).values(data={}))
    await session.execute(
        update(SequenceEnrollment)
        .where(
            SequenceEnrollment.business_id == bid,
            SequenceEnrollment.contact_id == cid,
            SequenceEnrollment.status == "active",
        )
        .values(status="stopped", stopped_reason="erased")
    )
    await append(
        session,
        business_id=bid,
        type="gtm.contact.erased",
        actor=actor,
        subject=("contact", cid),
        data={"id": str(cid)},
    )


async def handle_request(
    session: AsyncSession, services: Services, request: DataRequest, *, actor: Actor
) -> DataRequest:
    contact = await session.get(Contact, request.contact_id) if request.contact_id else None
    if contact is None:
        request.status, request.completed_at = "not_found", utcnow()
        request.result = {"note": "No personal data held for this person."}
        return request
    if request.kind == "export":
        data = await export_contact(session, contact)
        key = f"exports/{request.business_id}/{request.id}.json"
        await services.blobs.put(key, orjson.dumps(data, option=orjson.OPT_INDENT_2), "application/json")
        request.result = {"download_url": services.blobs.signed_url(key, ttl_s=7 * 86400), "expires_in_days": 7}
    else:
        await erase_contact(session, contact, actor=actor)
        request.result = {"erased": True}
    request.status, request.completed_at = "completed", utcnow()
    await append(
        session,
        business_id=request.business_id,
        type="gtm.data_request.done",
        actor=actor,
        subject=("data_request", request.id),
        data={"request_id": str(request.id), "kind": request.kind, "status": request.status},
    )
    return request


async def create_request(
    session: AsyncSession,
    services: Services,
    business_id: uuid.UUID,
    *,
    kind: str,
    contact_id: uuid.UUID | None,
    email: str | None,
    source: str,
    actor: Actor,
) -> DataRequest:
    contact = await session.get(Contact, contact_id) if contact_id else None
    if contact is None and email:
        contact = await session.scalar(
            select(Contact).where(Contact.business_id == business_id, Contact.email == email.strip().lower())
        )
    if contact_id and contact is None:
        raise NotFound("Contact not found")
    request = DataRequest(
        business_id=business_id,
        kind=kind,
        contact_id=contact.id if contact else None,
        email_hash=email_hash(email) if email else None,
        source=source,
        status="pending",
        confirmation_code=short_code(12),
    )
    session.add(request)
    await session.flush()
    return await handle_request(session, services, request, actor=actor)


def parse_signed_request(signed_request: str, app_secret: str) -> dict[str, Any] | None:
    """Meta's data-deletion callback: base64url(signature).base64url(payload), HMAC-SHA256 with the app secret."""
    try:
        encoded_sig, payload = signed_request.split(".", 1)
        sig = base64.urlsafe_b64decode(encoded_sig + "=" * (-len(encoded_sig) % 4))
        data: dict[str, Any] = orjson.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, orjson.JSONDecodeError):
        return None
    expected = hmac.new(app_secret.encode(), payload.encode(), hashlib.sha256).digest()
    if data.get("algorithm", "").upper() != "HMAC-SHA256" or not hmac.compare_digest(sig, expected):
        return None
    return data


def request_view(r: DataRequest) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "kind": r.kind,
        "contact_id": str(r.contact_id) if r.contact_id else None,
        "source": r.source,
        "status": r.status,
        "confirmation_code": r.confirmation_code,
        "requested_at": r.requested_at.isoformat(),
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        "result": r.result,
    }
