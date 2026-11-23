"""Nurture sequences: enrollment, rendering and the send pipeline.

Templates are editable by the business, so they render in Jinja's sandbox. Every send passes
the policy engine; consent is checked at enrollment *and* again right before sending.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from datetime import timedelta
from typing import Any

from jinja2.sandbox import SandboxedEnvironment
from markupsafe import Markup, escape
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.time import utcnow
from projectx.modules.gtm.models import Message, Sequence, SequenceEnrollment
from projectx.services import Services
from projectx.spine.mail import render
from projectx.spine.tenancy.models import Business
from projectx.spine.twin.models import Contact

_sandbox = SandboxedEnvironment(autoescape=False)


def _sign(secret: str, raw: bytes) -> bytes:
    return hmac.new(secret.encode(), b"unsub:" + raw, hashlib.sha256).digest()[:16]


def unsubscribe_token(secret: str, business_id: uuid.UUID, contact_id: uuid.UUID) -> str:
    raw = business_id.bytes + contact_id.bytes
    return base64.urlsafe_b64encode(raw + _sign(secret, raw)).decode().rstrip("=")


def parse_unsubscribe_token(secret: str, token: str) -> tuple[uuid.UUID, uuid.UUID] | None:
    try:
        data = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (ValueError, TypeError):
        return None
    if len(data) != 48:
        return None
    raw, sig = data[:32], data[32:]
    if not hmac.compare_digest(_sign(secret, raw), sig):
        return None
    return uuid.UUID(bytes=raw[:16]), uuid.UUID(bytes=raw[16:])


def first_name(contact: Contact) -> str:
    if contact.name:
        return contact.name.split()[0]
    return "there"


def template_context(services: Services, business: Business, contact: Contact) -> dict[str, Any]:
    token = unsubscribe_token(services.settings.secret_key.get_secret_value(), business.id, contact.id)
    base = services.settings.base_url
    return {
        "first_name": first_name(contact),
        "name": contact.name or "",
        "business_name": business.name,
        "business_website": business.website or "",
        "unsubscribe_url": f"{base}/u/{token}",
        "referral_link": f"{base}/r/{business.slug}/{contact.referral_code}" if contact.referral_code else "",
    }


def render_text(template: str, context: dict[str, Any]) -> str:
    return _sandbox.from_string(template).render(**context)


def text_to_html(text: str) -> Markup:
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    # Safe: every paragraph is escaped before it is wrapped in markup.
    return Markup("".join(f"<p>{escape(p).replace(chr(10), Markup('<br>'))}</p>" for p in paragraphs))  # noqa: S704


def build_email(
    services: Services, business: Business, contact: Contact, *, subject: str, body: str, preheader: str = ""
) -> dict[str, Any]:
    ctx = template_context(services, business, contact)
    rendered_subject = render_text(subject, ctx)
    rendered_body = render_text(body, ctx)
    address = (business.settings or {}).get("postal_address", "")
    html = render(
        "email/business.html",
        business_name=business.name,
        preheader=preheader,
        body_html=text_to_html(rendered_body),
        unsubscribe_url=ctx["unsubscribe_url"],
        postal_address=address,
    )
    text = f"{rendered_body}\n\n—\n{business.name}\n{address}\nUnsubscribe: {ctx['unsubscribe_url']}".strip()
    headers = {
        "List-Unsubscribe": f"<{ctx['unsubscribe_url']}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    return {"subject": rendered_subject, "html": html, "text": text, "headers": headers}


async def enroll(
    session: AsyncSession,
    business: Business,
    contact: Contact,
    sequence_key: str,
    *,
    cause: dict[str, Any] | None = None,
) -> SequenceEnrollment | None:
    from projectx.modules.gtm.crm.lifecycle import has_consent

    seq = await session.scalar(
        select(Sequence).where(
            Sequence.business_id == business.id, Sequence.key == sequence_key, Sequence.active.is_(True)
        )
    )
    if seq is None or not seq.steps:
        return None
    if not await has_consent(session, contact, seq.channel):
        return None
    existing = await session.scalar(
        select(SequenceEnrollment.id).where(
            SequenceEnrollment.sequence_id == seq.id,
            SequenceEnrollment.contact_id == contact.id,
            SequenceEnrollment.status == "active",
        )
    )
    if existing is not None:
        return None
    delay = float(seq.steps[0].get("delay_hours", 0))
    enrollment = SequenceEnrollment(
        business_id=business.id,
        sequence_id=seq.id,
        contact_id=contact.id,
        status="active",
        step_index=0,
        next_run_at=utcnow() + timedelta(hours=delay),
        cause=cause or {},
    )
    session.add(enrollment)
    await session.flush()
    return enrollment


async def stop_enrollments(session: AsyncSession, business_id: uuid.UUID, contact_id: uuid.UUID, *, reason: str) -> int:
    result = await session.execute(
        update(SequenceEnrollment)
        .where(
            SequenceEnrollment.business_id == business_id,
            SequenceEnrollment.contact_id == contact_id,
            SequenceEnrollment.status == "active",
        )
        .values(status="stopped", stopped_reason=reason[:200])
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def should_stop(seq: Sequence, contact: Contact) -> str | None:
    stop_on = seq.stop_on or {}
    if contact.side_state in ("do_not_contact", "disqualified", "churned") and seq.key != "win_back":
        return f"contact is {contact.side_state}"
    stages = stop_on.get("stages") or []
    if contact.stage in stages:
        return f"contact reached {contact.stage}"
    return None


async def queue_message(
    session: AsyncSession,
    services: Services,
    business: Business,
    contact: Contact,
    *,
    channel: str,
    kind: str,
    subject: str,
    body: str,
    sequence_id: uuid.UUID | None = None,
    enrollment_id: uuid.UUID | None = None,
    issue_id: uuid.UUID | None = None,
) -> Message:
    built = (
        build_email(services, business, contact, subject=subject, body=body)
        if channel == "email"
        else {"subject": None, "text": render_text(body, template_context(services, business, contact)), "html": None}
    )
    message = Message(
        business_id=business.id,
        contact_id=contact.id,
        channel=channel,
        kind=kind,
        sequence_id=sequence_id,
        enrollment_id=enrollment_id,
        issue_id=issue_id,
        subject=built["subject"],
        body_text=built["text"],
        body_html=built.get("html"),
        status="queued",
    )
    session.add(message)
    await session.flush()
    return message
