"""The funnel as a state machine.

Every contact always has a stage (where they are) and a status (what's happening within the
stage). Every change is written to ``contact_stage_changes`` and the ledger with what caused it
(which campaign, post, email or payment): that history is what the learning loop learns from.
Stages only move forward automatically; moving back is a deliberate, manual act.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.errors import ValidationFailed
from projectx.kernel.ids import short_code
from projectx.kernel.time import utcnow
from projectx.modules.gtm.config import gtm_settings
from projectx.spine.jobs.queue import enqueue
from projectx.spine.ledger.service import Actor, append
from projectx.spine.tenancy.models import Business
from projectx.spine.twin.models import SIDE_STATES, STAGES, STATUSES, Consent, Contact, ContactStageChange, Touch

STAGE_INDEX = {stage: i for i, stage in enumerate(STAGES)}
_UNSET: Any = object()

# Touches that imply a minimum stage (the "Enters when" column of the GTM plan).
TOUCH_STAGE_FLOOR = {
    "signup": "subscriber",
    "discord_join": "subscriber",
    "form_submit": "lead",
    "download": "lead",
    "reply": "lead",
    "dm": "lead",
    "comment": "lead",
    "pricing_view": "lead",
    "webinar_register": "lead",
    "meeting_booked": "sql",
    "price_request": "sql",
    "meeting": "opportunity",
    "proposal": "opportunity",
    "payment": "customer",
    "referral": "advocate",
    "review": "advocate",
}

# Lead-score points per touch. Scores decay with a half-life (see recompute_score).
TOUCH_POINTS = {
    "page_view": 2,
    "link_click": 5,
    "email_open": 1,
    "email_click": 5,
    "signup": 10,
    "form_submit": 20,
    "download": 20,
    "comment": 5,
    "reply": 15,
    "dm": 15,
    "pricing_view": 15,
    "discord_join": 8,
    "community_post": 4,
    "webinar_register": 15,
    "webinar_attend": 25,
    "meeting_booked": 30,
    "price_request": 30,
    "link_click_anonymous": 0,
    "referral": 20,
    "review": 15,
}


@dataclass(frozen=True)
class Cause:
    type: str
    id: uuid.UUID | None = None
    actor: Actor = field(default_factory=lambda: Actor.system("gtm"))
    reason: str | None = None


@dataclass
class Attribution:
    campaign_id: uuid.UUID | None = None
    platform: str | None = None
    post_id: uuid.UUID | None = None
    link_id: uuid.UUID | None = None
    content_id: uuid.UUID | None = None
    utm: dict[str, Any] = field(default_factory=dict)

    def as_touch(self, at: datetime) -> dict[str, Any]:
        return {
            "campaign_id": str(self.campaign_id) if self.campaign_id else None,
            "platform": self.platform,
            "post_id": str(self.post_id) if self.post_id else None,
            "link_id": str(self.link_id) if self.link_id else None,
            "content_id": str(self.content_id) if self.content_id else None,
            "utm": self.utm,
            "at": at.isoformat(),
        }


@dataclass
class ConsentInput:
    channel: str  # email | whatsapp | sms | discord
    purpose: str  # marketing | newsletter
    source: str
    evidence: dict[str, Any] = field(default_factory=dict)


def _validate(stage: str | None, status: str | None, side_state: str | None) -> None:
    if stage is not None and stage not in STAGE_INDEX:
        raise ValidationFailed(f"Unknown stage {stage!r}")
    if status is not None and status not in STATUSES:
        raise ValidationFailed(f"Unknown status {status!r}")
    if side_state is not None and side_state not in SIDE_STATES:
        raise ValidationFailed(f"Unknown side state {side_state!r}")


async def change(
    session: AsyncSession,
    contact: Contact,
    *,
    cause: Cause,
    stage: str | None = None,
    status: str | None = None,
    side_state: Any = _UNSET,
    allow_backward: bool = False,
    reason: str | None = None,
) -> bool:
    """Move a contact. Returns True when anything changed. Triggers automations after commit."""
    _validate(stage, status, side_state if side_state is not _UNSET else None)
    new_stage = contact.stage
    if (
        stage is not None
        and stage != contact.stage
        and (STAGE_INDEX[stage] > STAGE_INDEX[contact.stage] or allow_backward)
    ):
        new_stage = stage
    new_status = status or contact.status
    new_side = contact.side_state if side_state is _UNSET else side_state
    if new_stage == contact.stage and new_status == contact.status and new_side == contact.side_state:
        return False
    previous = (contact.stage, contact.status, contact.side_state)
    stage_changed = new_stage != contact.stage
    side_changed = new_side != contact.side_state
    contact.stage, contact.status, contact.side_state = new_stage, new_status, new_side
    if stage_changed:
        contact.stage_entered_at = utcnow()
    if reason:
        contact.status_reason = reason[:300]
    session.add(
        ContactStageChange(
            business_id=contact.business_id,
            contact_id=contact.id,
            from_stage=previous[0],
            to_stage=new_stage,
            from_status=previous[1],
            to_status=new_status,
            from_side_state=previous[2],
            to_side_state=new_side,
            cause_type=cause.type,
            cause_id=cause.id,
            actor=cause.actor.label(),
            reason=reason or cause.reason,
        )
    )
    await append(
        session,
        business_id=contact.business_id,
        type="gtm.contact.stage_changed",
        actor=cause.actor,
        subject=("contact", contact.id),
        data={
            "contact_id": str(contact.id),
            "from_stage": previous[0],
            "to_stage": new_stage,
            "from_status": previous[1],
            "to_status": new_status,
            "side_state": new_side,
            "cause_type": cause.type,
            "cause_id": str(cause.id) if cause.id else None,
        },
    )
    if stage_changed:
        await enqueue(
            session,
            "gtm.run_automations",
            {"contact_id": str(contact.id), "event": "stage_entered", "value": new_stage},
            business_id=contact.business_id,
        )
    if side_changed and new_side:
        await enqueue(
            session,
            "gtm.run_automations",
            {"contact_id": str(contact.id), "event": "side_state_entered", "value": new_side},
            business_id=contact.business_id,
        )
    return True


async def find_contact(
    session: AsyncSession,
    business_id: uuid.UUID,
    *,
    email: str | None = None,
    phone: str | None = None,
    handle: tuple[str, str] | None = None,
) -> Contact | None:
    if email:
        found = await session.scalar(
            select(Contact).where(Contact.business_id == business_id, Contact.email == email.strip().lower())
        )
        if found:
            return found
    if handle:
        platform, value = handle
        found = await session.scalar(
            select(Contact).where(Contact.business_id == business_id, Contact.handles[platform].astext == value.lower())
        )
        found = found if isinstance(found, Contact) else None
        if found:
            return found
    if phone:
        by_phone: Contact | None = await session.scalar(
            select(Contact).where(Contact.business_id == business_id, Contact.phone == phone)
        )
        return by_phone
    return None


async def upsert_contact(
    session: AsyncSession,
    business: Business,
    *,
    source: str,
    cause: Cause,
    email: str | None = None,
    phone: str | None = None,
    name: str | None = None,
    handle: tuple[str, str] | None = None,
    stage: str = "lead",
    status: str | None = None,
    attribution: Attribution | None = None,
    consent: ConsentInput | None = None,
    attributes: dict[str, Any] | None = None,
    referred_by_code: str | None = None,
) -> tuple[Contact, bool]:
    if not (email or phone or handle):
        raise ValidationFailed("A contact needs an email, a phone number or a platform handle")
    _validate(stage, status, None)
    contact = await find_contact(session, business.id, email=email, phone=phone, handle=handle)
    now = utcnow()
    created = contact is None
    if contact is None:
        contact = Contact(
            business_id=business.id,
            email=email.strip().lower() if email else None,
            phone=phone,
            name=(name or "").strip()[:200] or None,
            handles={},
            stage=stage,
            status=status or "new",
            source=source,
            attributes={},
            tags=[],
            referral_code=short_code(8).upper(),
            stage_entered_at=now,
        )
        session.add(contact)
        await session.flush()
    if handle:
        contact.handles = {**(contact.handles or {}), handle[0]: handle[1].lower()}
    if name and not contact.name:
        contact.name = name.strip()[:200]
    if email and not contact.email:
        contact.email = email.strip().lower()
    if phone and not contact.phone:
        contact.phone = phone
    if attributes:
        contact.attributes = {**(contact.attributes or {}), **attributes}
    if attribution is not None:
        touch = attribution.as_touch(now)
        if contact.first_touch is None:
            contact.first_touch = touch
            contact.first_campaign_id = attribution.campaign_id
        contact.last_touch = touch
        if attribution.campaign_id:
            contact.last_campaign_id = attribution.campaign_id
    if referred_by_code and created:
        referrer = await session.scalar(
            select(Contact).where(Contact.business_id == business.id, Contact.referral_code == referred_by_code.upper())
        )
        if referrer is not None and referrer.id != contact.id:
            contact.referred_by_id = referrer.id
            await record_touch(session, business, referrer, kind="referral", data={"referred": str(contact.id)})
    contact.last_activity_at = now
    if created:
        await append(
            session,
            business_id=business.id,
            type="gtm.contact.created",
            actor=cause.actor,
            subject=("contact", contact.id),
            data={
                "contact_id": str(contact.id),
                "source": source,
                "stage": contact.stage,
                "campaign_id": str(attribution.campaign_id) if attribution and attribution.campaign_id else None,
            },
        )
        await enqueue(
            session,
            "gtm.run_automations",
            {"contact_id": str(contact.id), "event": "stage_entered", "value": contact.stage},
            business_id=business.id,
        )
    elif contact.side_state != "do_not_contact":
        await change(session, contact, cause=cause, stage=stage, status=status)
    if consent is not None:
        await grant_consent(session, contact, consent, actor=cause.actor)
    return contact, created


async def grant_consent(session: AsyncSession, contact: Contact, consent: ConsentInput, *, actor: Actor) -> None:
    session.add(
        Consent(
            business_id=contact.business_id,
            contact_id=contact.id,
            channel=consent.channel,
            purpose=consent.purpose,
            status="granted",
            source=consent.source,
            evidence=consent.evidence,
        )
    )
    if consent.channel == "email":
        contact.unsubscribed_at = None
    await append(
        session,
        business_id=contact.business_id,
        type="gtm.consent.changed",
        actor=actor,
        subject=("contact", contact.id),
        data={
            "contact_id": str(contact.id),
            "channel": consent.channel,
            "purpose": consent.purpose,
            "status": "granted",
            "source": consent.source,
        },
    )


async def revoke_consent(session: AsyncSession, contact: Contact, *, channel: str, source: str, actor: Actor) -> None:
    session.add(
        Consent(
            business_id=contact.business_id,
            contact_id=contact.id,
            channel=channel,
            purpose="marketing",
            status="revoked",
            source=source,
            evidence={},
        )
    )
    if channel == "email":
        contact.unsubscribed_at = utcnow()
    await append(
        session,
        business_id=contact.business_id,
        type="gtm.consent.changed",
        actor=actor,
        subject=("contact", contact.id),
        data={
            "contact_id": str(contact.id),
            "channel": channel,
            "purpose": "marketing",
            "status": "revoked",
            "source": source,
        },
    )


async def has_consent(session: AsyncSession, contact: Contact, channel: str) -> bool:
    if contact.side_state == "do_not_contact" or contact.erased_at is not None:
        return False
    if channel == "email" and (
        contact.unsubscribed_at is not None or contact.email_status in ("bounced", "complained")
    ):
        return False
    latest = await session.scalar(
        select(Consent.status)
        .where(Consent.business_id == contact.business_id, Consent.contact_id == contact.id, Consent.channel == channel)
        .order_by(Consent.recorded_at.desc())
        .limit(1)
    )
    return latest == "granted"


async def record_touch(
    session: AsyncSession,
    business: Business,
    contact: Contact,
    *,
    kind: str,
    channel: str | None = None,
    campaign_id: uuid.UUID | None = None,
    content_id: uuid.UUID | None = None,
    post_id: uuid.UUID | None = None,
    link_id: uuid.UUID | None = None,
    data: dict[str, Any] | None = None,
    cause: Cause | None = None,
) -> Touch:
    points = TOUCH_POINTS.get(kind, 0)
    touch = Touch(
        business_id=business.id,
        contact_id=contact.id,
        kind=kind,
        channel=channel,
        campaign_id=campaign_id,
        content_id=content_id,
        post_id=post_id,
        link_id=link_id,
        points=points,
        data=data or {},
    )
    session.add(touch)
    contact.last_activity_at = utcnow()
    contact.lead_score = (contact.lead_score or 0) + points
    cause = cause or Cause(type=_cause_type_for(kind), id=post_id or link_id or campaign_id)
    floor = TOUCH_STAGE_FLOOR.get(kind)
    if floor and contact.side_state != "do_not_contact":
        await change(session, contact, cause=cause, stage=floor, status="engaged" if floor in ("lead", "mql") else None)
    threshold = int(gtm_settings(business)["mql_threshold"])
    if contact.lead_score >= threshold and STAGE_INDEX[contact.stage] < STAGE_INDEX["mql"]:
        await change(
            session,
            contact,
            cause=Cause("automation", reason="lead score passed the threshold"),
            stage="mql",
            status="engaged",
        )
    await session.flush()
    return touch


def _cause_type_for(kind: str) -> str:
    if kind.startswith("email"):
        return "email"
    if kind in ("link_click",):
        return "link"
    if kind in ("form_submit", "page_view", "download", "signup"):
        return "landing_page"
    if kind == "payment":
        return "payment"
    if kind in ("comment", "dm", "reply"):
        return "inbox"
    return "touch"


async def recompute_score(session: AsyncSession, business: Business, contact: Contact) -> int:
    """Decay old engagement: score = sum(points * 0.5 ** (age_days / half_life))."""
    half_life = float(gtm_settings(business)["score_half_life_days"])
    age_days = func.extract("epoch", func.now() - Touch.occurred_at) / 86400.0
    total = await session.scalar(
        select(func.coalesce(func.sum(Touch.points * func.power(0.5, age_days / half_life)), 0)).where(
            Touch.business_id == business.id, Touch.contact_id == contact.id
        )
    )
    contact.lead_score = round(float(total or 0))
    return contact.lead_score


def contact_view(c: Contact) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "email": c.email,
        "phone": c.phone,
        "name": c.name,
        "company": c.company,
        "job_title": c.job_title,
        "handles": c.handles,
        "stage": c.stage,
        "side_state": c.side_state,
        "status": c.status,
        "status_reason": c.status_reason,
        "customer_type": c.customer_type,
        "source": c.source,
        "first_touch": c.first_touch,
        "last_touch": c.last_touch,
        "first_campaign_id": str(c.first_campaign_id) if c.first_campaign_id else None,
        "last_campaign_id": str(c.last_campaign_id) if c.last_campaign_id else None,
        "lead_score": c.lead_score,
        "owner_type": c.owner_type,
        "owner_user_id": str(c.owner_user_id) if c.owner_user_id else None,
        "next_action": c.next_action,
        "next_action_at": c.next_action_at.isoformat() if c.next_action_at else None,
        "tags": c.tags,
        "attributes": c.attributes,
        "email_status": c.email_status,
        "unsubscribed": c.unsubscribed_at is not None,
        "last_activity_at": c.last_activity_at.isoformat() if c.last_activity_at else None,
        "stage_entered_at": c.stage_entered_at.isoformat() if c.stage_entered_at else None,
        "lifetime_value_minor": c.lifetime_value_minor,
        "referral_code": c.referral_code,
        "created_at": c.created_at.isoformat(),
        "erased": c.erased_at is not None,
    }
