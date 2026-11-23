"""CRM endpoints: contacts, the funnel, deals and payments."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, File, Form, UploadFile, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import Text, func, or_, select, text
from sqlalchemy import cast as sa_cast

from projectx.api.deps import BusinessDep, EditorDep
from projectx.kernel.errors import NotFound, ValidationFailed
from projectx.kernel.pagination import clamp_limit, decode_cursor, encode_cursor
from projectx.modules.gtm.crm.deals import (
    PIPELINE_TEMPLATES,
    create_deal,
    deal_view,
    ensure_pipeline,
    move_deal,
    pipeline_view,
)
from projectx.modules.gtm.crm.lifecycle import (
    Cause,
    ConsentInput,
    change,
    contact_view,
    upsert_contact,
)
from projectx.modules.gtm.crm.payments import payment_view, record_payment
from projectx.modules.gtm.models import Message, SequenceEnrollment
from projectx.spine.jobs.queue import enqueue
from projectx.spine.ledger.service import Actor
from projectx.spine.twin.models import STAGES, Contact, ContactStageChange, Deal, Payment, Pipeline, Touch

router = APIRouter(prefix="/api/v1/b/{business_id}/gtm", tags=["gtm: crm"])

Stage = Literal[
    "audience", "subscriber", "lead", "mql", "sql", "opportunity", "customer", "active", "expansion", "advocate"
]
Status = Literal["new", "contacted", "engaged", "no_response", "meeting_scheduled", "proposal_sent", "won", "lost"]
SideState = Literal["disqualified", "recycled", "at_risk", "churned", "do_not_contact"]


class HandleIn(BaseModel):
    platform: str = Field(min_length=1, max_length=30)
    value: str = Field(min_length=1, max_length=200)


class ConsentIn(BaseModel):
    channel: Literal["email", "whatsapp", "sms"] = "email"
    purpose: Literal["marketing", "newsletter"] = "marketing"
    evidence: str = Field(min_length=3, max_length=500, description="How and when this person agreed")


class ContactIn(BaseModel):
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32)
    name: str | None = Field(default=None, max_length=200)
    company: str | None = Field(default=None, max_length=200)
    job_title: str | None = Field(default=None, max_length=200)
    handle: HandleIn | None = None
    stage: Stage = "lead"
    status: Status | None = None
    tags: list[str] = Field(default_factory=list, max_length=20)
    consent: ConsentIn | None = None


class ContactPatch(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    company: str | None = Field(default=None, max_length=200)
    job_title: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    tags: list[str] | None = Field(default=None, max_length=20)
    stage: Stage | None = None
    status: Status | None = None
    side_state: SideState | Literal["none"] | None = None
    reason: str | None = Field(default=None, max_length=300)
    owner_type: Literal["ai", "human"] | None = None
    next_action: str | None = Field(default=None, max_length=300)
    next_action_at: str | None = None
    customer_type: Literal["one_time", "retainer", "subscription"] | None = None
    satisfaction: int | None = Field(default=None, ge=0, le=10)


@router.get("/contacts")
async def list_contacts(
    ctx: BusinessDep,
    stage: str | None = None,
    status: str | None = None,
    side_state: str | None = None,
    source: str | None = None,
    q: str | None = None,
    campaign_id: uuid.UUID | None = None,
    sort: Literal["recent", "score", "activity"] = "recent",
    cursor: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    n = clamp_limit(limit)
    filters = [Contact.business_id == ctx.business_id, Contact.erased_at.is_(None)]
    if stage:
        filters.append(Contact.stage.in_(stage.split(",")))
    if status:
        filters.append(Contact.status.in_(status.split(",")))
    if side_state:
        filters.append(
            Contact.side_state.is_(None) if side_state == "none" else Contact.side_state.in_(side_state.split(","))
        )
    if source:
        filters.append(Contact.source == source)
    if campaign_id:
        filters.append(or_(Contact.first_campaign_id == campaign_id, Contact.last_campaign_id == campaign_id))
    if q:
        like = f"%{q.strip()}%"
        filters.append(
            or_(
                Contact.name.ilike(like),
                Contact.email.ilike(like),
                Contact.company.ilike(like),
                sa_cast(Contact.handles, Text).ilike(like),
            )
        )
    total = await ctx.session.scalar(select(func.count()).select_from(Contact).where(*filters))
    stmt = select(Contact).where(*filters)
    if sort == "score":
        stmt = stmt.order_by(Contact.lead_score.desc(), Contact.id.desc())
    elif sort == "activity":
        stmt = stmt.order_by(Contact.last_activity_at.desc().nulls_last(), Contact.id.desc())
    else:
        if cursor:
            ts, last_id = decode_cursor(cursor)
            stmt = stmt.where(or_(Contact.created_at < ts, (Contact.created_at == ts) & (Contact.id < last_id)))
        stmt = stmt.order_by(Contact.created_at.desc(), Contact.id.desc())
    rows = list((await ctx.session.execute(stmt.limit(n + 1))).scalars())
    next_cursor = None
    if len(rows) > n and sort == "recent":
        next_cursor = encode_cursor(rows[n - 1].created_at, rows[n - 1].id)
    return {"items": [contact_view(c) for c in rows[:n]], "next_cursor": next_cursor, "total": int(total or 0)}


@router.post("/contacts", status_code=status.HTTP_201_CREATED)
async def create_contact(body: ContactIn, ctx: EditorDep) -> dict[str, Any]:
    actor = Actor.user(ctx.user.id)
    consent = None
    if body.consent:
        consent = ConsentInput(
            channel=body.consent.channel,
            purpose=body.consent.purpose,
            source="manual",
            evidence={"text": body.consent.evidence, "recorded_by": str(ctx.user.id)},
        )
    contact, created = await upsert_contact(
        ctx.session,
        ctx.business,
        source="manual",
        cause=Cause("manual", actor=actor),
        email=str(body.email) if body.email else None,
        phone=body.phone,
        name=body.name,
        handle=(body.handle.platform, body.handle.value) if body.handle else None,
        stage=body.stage,
        status=body.status,
        consent=consent,
    )
    contact.company = body.company or contact.company
    contact.job_title = body.job_title or contact.job_title
    if body.tags:
        contact.tags = sorted(set(contact.tags or []) | {t.strip()[:50] for t in body.tags if t.strip()})
    contact.owner_type = "human" if not created else contact.owner_type
    return {**contact_view(contact), "created": created}


@router.get("/contacts/{contact_id}")
async def get_contact(contact_id: uuid.UUID, ctx: BusinessDep) -> dict[str, Any]:
    contact = await ctx.session.get(Contact, contact_id)
    if contact is None:
        raise NotFound("Contact not found")
    s = ctx.session
    changes = (
        (
            await s.execute(
                select(ContactStageChange)
                .where(ContactStageChange.contact_id == contact_id)
                .order_by(ContactStageChange.changed_at.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    touches = (
        (
            await s.execute(
                select(Touch).where(Touch.contact_id == contact_id).order_by(Touch.occurred_at.desc()).limit(100)
            )
        )
        .scalars()
        .all()
    )
    messages = (
        (
            await s.execute(
                select(Message).where(Message.contact_id == contact_id).order_by(Message.created_at.desc()).limit(50)
            )
        )
        .scalars()
        .all()
    )
    deals = (
        (await s.execute(select(Deal).where(Deal.contact_id == contact_id).order_by(Deal.created_at.desc())))
        .scalars()
        .all()
    )
    payments = (
        (await s.execute(select(Payment).where(Payment.contact_id == contact_id).order_by(Payment.paid_at.desc())))
        .scalars()
        .all()
    )
    enrollments = (
        (await s.execute(select(SequenceEnrollment).where(SequenceEnrollment.contact_id == contact_id))).scalars().all()
    )
    timeline: list[dict[str, Any]] = []
    for ch in changes:
        timeline.append(
            {
                "type": "stage",
                "at": ch.changed_at.isoformat(),
                "from_stage": ch.from_stage,
                "to_stage": ch.to_stage,
                "from_status": ch.from_status,
                "to_status": ch.to_status,
                "side_state": ch.to_side_state,
                "cause_type": ch.cause_type,
                "cause_id": str(ch.cause_id) if ch.cause_id else None,
                "reason": ch.reason,
            }
        )
    for t in touches:
        timeline.append(
            {
                "type": "touch",
                "at": t.occurred_at.isoformat(),
                "kind": t.kind,
                "channel": t.channel,
                "points": t.points,
                "campaign_id": str(t.campaign_id) if t.campaign_id else None,
                "post_id": str(t.post_id) if t.post_id else None,
            }
        )
    for m in messages:
        timeline.append(
            {
                "type": "message",
                "at": m.created_at.isoformat(),
                "channel": m.channel,
                "kind": m.kind,
                "subject": m.subject,
                "status": m.status,
            }
        )
    timeline.sort(key=lambda e: e["at"], reverse=True)
    return {
        **contact_view(contact),
        "timeline": timeline[:200],
        "deals": [deal_view(d) for d in deals],
        "payments": [payment_view(p) for p in payments],
        "enrollments": [
            {
                "id": str(e.id),
                "sequence_id": str(e.sequence_id),
                "status": e.status,
                "step_index": e.step_index,
                "next_run_at": e.next_run_at.isoformat() if e.next_run_at else None,
            }
            for e in enrollments
        ],
    }


@router.patch("/contacts/{contact_id}")
async def update_contact(contact_id: uuid.UUID, body: ContactPatch, ctx: EditorDep) -> dict[str, Any]:
    from datetime import datetime

    contact = await ctx.session.get(Contact, contact_id)
    if contact is None or contact.erased_at is not None:
        raise NotFound("Contact not found")
    data = body.model_dump(exclude_unset=True)
    for key in ("name", "company", "job_title", "phone", "owner_type", "next_action", "customer_type"):
        if key in data:
            setattr(contact, key, data[key])
    if "tags" in data and data["tags"] is not None:
        contact.tags = sorted({t.strip()[:50] for t in data["tags"] if t.strip()})
    if "next_action_at" in data:
        contact.next_action_at = datetime.fromisoformat(data["next_action_at"]) if data["next_action_at"] else None
    if body.owner_type == "human":
        contact.owner_user_id = ctx.user.id
    if any(k in data for k in ("stage", "status", "side_state")):
        side: Any = None if body.side_state == "none" else body.side_state
        kwargs: dict[str, Any] = {"stage": body.stage, "status": body.status}
        if "side_state" in data:
            kwargs["side_state"] = side
        await change(
            ctx.session,
            contact,
            cause=Cause("manual", actor=Actor.user(ctx.user.id)),
            allow_backward=True,
            reason=body.reason,
            **kwargs,
        )
    if body.satisfaction is not None:
        contact.attributes = {**(contact.attributes or {}), "satisfaction": body.satisfaction}
        await enqueue(
            ctx.session,
            "gtm.run_automations",
            {"contact_id": str(contact.id), "event": "satisfaction", "value": body.satisfaction},
            business_id=ctx.business_id,
        )
    return contact_view(contact)


@router.post("/contacts/import")
async def import_contacts(
    ctx: EditorDep, file: UploadFile = File(...), consent_confirmed: bool = Form(False), stage: str = Form("subscriber")
) -> dict[str, Any]:
    if stage not in STAGES:
        raise ValidationFailed("Unknown stage")
    raw = await file.read(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024:
        raise ValidationFailed("Files up to 5 MB, please")
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig", errors="replace")))
    created = updated = skipped = 0
    actor = Actor.user(ctx.user.id)
    for i, row in enumerate(reader):
        if i >= 20_000:
            break
        lowered = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        email = lowered.get("email") or None
        if not email or "@" not in email:
            skipped += 1
            continue
        consent = (
            ConsentInput(
                channel="email",
                purpose="marketing",
                source="import",
                evidence={"file": file.filename, "confirmed_by": str(ctx.user.id)},
            )
            if consent_confirmed
            else None
        )
        contact, was_created = await upsert_contact(
            ctx.session,
            ctx.business,
            source="import",
            cause=Cause("import", actor=actor),
            email=email,
            name=lowered.get("name") or None,
            phone=lowered.get("phone") or None,
            stage=stage,
            consent=consent,
        )
        if lowered.get("company"):
            contact.company = lowered["company"][:200]
        if lowered.get("tags"):
            contact.tags = sorted(set(contact.tags or []) | {t.strip()[:50] for t in lowered["tags"].split(";") if t})
        created += int(was_created)
        updated += int(not was_created)
    return {"created": created, "updated": updated, "skipped": skipped, "consent_recorded": consent_confirmed}


@router.get("/funnel")
async def funnel(ctx: BusinessDep) -> dict[str, Any]:
    s, bid = ctx.session, ctx.business_id
    counts = dict(
        (
            await s.execute(
                select(Contact.stage, func.count())
                .where(Contact.business_id == bid, Contact.erased_at.is_(None))
                .group_by(Contact.stage)
            )
        )
        .tuples()
        .all()
    )
    sides = dict(
        (
            await s.execute(
                select(Contact.side_state, func.count())
                .where(Contact.business_id == bid, Contact.side_state.is_not(None), Contact.erased_at.is_(None))
                .group_by(Contact.side_state)
            )
        )
        .tuples()
        .all()
    )
    reached: dict[str, int] = {}
    running = 0
    for stage_key in reversed(STAGES):
        running += int(counts.get(stage_key, 0))
        reached[stage_key] = running
    durations = await s.execute(
        text(
            """
        SELECT stage, percentile_cont(0.5) WITHIN GROUP (ORDER BY secs) AS median_secs FROM (
          SELECT to_stage AS stage,
                 EXTRACT(EPOCH FROM (LEAD(changed_at) OVER (PARTITION BY contact_id ORDER BY changed_at)
                                     - changed_at)) AS secs
          FROM contact_stage_changes WHERE business_id = :b AND from_stage IS DISTINCT FROM to_stage
        ) t WHERE secs IS NOT NULL GROUP BY stage
        """
        ),
        {"b": bid},
    )
    median_days = {row.stage: round(float(row.median_secs) / 86400, 1) for row in durations}
    stages = []
    for i, stage_key in enumerate(STAGES):
        nxt = STAGES[i + 1] if i + 1 < len(STAGES) else None
        conversion = (reached[nxt] / reached[stage_key]) if nxt and reached[stage_key] else None
        stages.append(
            {
                "stage": stage_key,
                "count": int(counts.get(stage_key, 0)),
                "reached": reached[stage_key],
                "conversion_to_next": round(conversion, 4) if conversion is not None else None,
                "median_days_in_stage": median_days.get(stage_key),
            }
        )
    return {
        "stages": stages,
        "side_states": {k: int(v) for k, v in sides.items()},
        "total": sum(int(v) for v in counts.values()),
    }


# ------------------------------------------------------------------ deals


class PipelineIn(BaseModel):
    key: Literal["sales", "advertisers", "partners"]


class DealIn(BaseModel):
    pipeline_id: uuid.UUID
    title: str = Field(min_length=1, max_length=200)
    contact_id: uuid.UUID | None = None
    value_minor: int = Field(default=0, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    stage_key: str | None = None
    deal_type: Literal["one_time", "retainer", "subscription", "sponsorship", "renewal", "upsell"] = "one_time"
    expected_close: date | None = None
    campaign_id: uuid.UUID | None = None


class DealPatch(BaseModel):
    stage_key: str | None = None
    lost_reason: str | None = Field(default=None, max_length=300)
    title: str | None = Field(default=None, max_length=200)
    value_minor: int | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=10_000)
    next_action: str | None = Field(default=None, max_length=300)
    expected_close: date | None = None


@router.get("/pipelines")
async def pipelines(ctx: BusinessDep) -> dict[str, Any]:
    rows = (
        (
            await ctx.session.execute(
                select(Pipeline)
                .where(Pipeline.business_id == ctx.business_id)
                .order_by(Pipeline.is_default.desc(), Pipeline.created_at)
            )
        )
        .scalars()
        .all()
    )
    return {"items": [pipeline_view(p) for p in rows], "templates": sorted(PIPELINE_TEMPLATES)}


@router.post("/pipelines", status_code=status.HTTP_201_CREATED)
async def add_pipeline(body: PipelineIn, ctx: EditorDep) -> dict[str, Any]:
    return pipeline_view(await ensure_pipeline(ctx.session, ctx.business_id, body.key))


@router.get("/deals")
async def deals(ctx: BusinessDep, pipeline_id: uuid.UUID | None = None, status: str | None = None) -> dict[str, Any]:
    stmt = select(Deal).where(Deal.business_id == ctx.business_id)
    if pipeline_id:
        stmt = stmt.where(Deal.pipeline_id == pipeline_id)
    if status:
        stmt = stmt.where(Deal.status.in_(status.split(",")))
    rows = (await ctx.session.execute(stmt.order_by(Deal.stage_entered_at.desc()).limit(500))).scalars().all()
    contact_ids = {d.contact_id for d in rows if d.contact_id}
    names: dict[uuid.UUID, str | None] = {}
    if contact_ids:
        for cid, name, email in (
            await ctx.session.execute(
                select(Contact.id, Contact.name, Contact.email).where(Contact.id.in_(contact_ids))
            )
        ).all():
            names[cid] = name or email
    return {
        "items": [{**deal_view(d), "contact_name": names.get(d.contact_id) if d.contact_id else None} for d in rows]
    }


@router.post("/deals", status_code=status.HTTP_201_CREATED)
async def new_deal(body: DealIn, ctx: EditorDep) -> dict[str, Any]:
    pipeline = await ctx.session.get(Pipeline, body.pipeline_id)
    if pipeline is None:
        raise NotFound("Pipeline not found")
    contact = await ctx.session.get(Contact, body.contact_id) if body.contact_id else None
    deal = await create_deal(
        ctx.session,
        business_id=ctx.business_id,
        pipeline=pipeline,
        title=body.title,
        contact=contact,
        value_minor=body.value_minor,
        currency=(body.currency or ctx.business.currency).upper(),
        stage_key=body.stage_key,
        deal_type=body.deal_type,
        expected_close=body.expected_close,
        campaign_id=body.campaign_id or (contact.last_campaign_id if contact else None),
        actor=Actor.user(ctx.user.id),
    )
    return deal_view(deal)


@router.patch("/deals/{deal_id}")
async def update_deal(deal_id: uuid.UUID, body: DealPatch, ctx: EditorDep) -> dict[str, Any]:
    deal = await ctx.session.get(Deal, deal_id)
    if deal is None:
        raise NotFound("Deal not found")
    for key in ("title", "value_minor", "notes", "next_action", "expected_close"):
        value = getattr(body, key)
        if value is not None:
            setattr(deal, key, value)
    if body.stage_key and body.stage_key != deal.stage_key:
        await move_deal(
            ctx.session, deal, stage_key=body.stage_key, actor=Actor.user(ctx.user.id), lost_reason=body.lost_reason
        )
    return deal_view(deal)


# ------------------------------------------------------------------ payments


class PaymentIn(BaseModel):
    amount_minor: int
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    email: EmailStr | None = None
    contact_id: uuid.UUID | None = None
    kind: Literal["one_time", "subscription", "refund"] = "one_time"
    promo_code: str | None = Field(default=None, max_length=40)
    description: str | None = Field(default=None, max_length=300)
    reference: str | None = Field(default=None, max_length=200)


@router.get("/payments")
async def payments(ctx: BusinessDep, limit: int | None = None) -> dict[str, Any]:
    rows = (
        (
            await ctx.session.execute(
                select(Payment)
                .where(Payment.business_id == ctx.business_id)
                .order_by(Payment.paid_at.desc())
                .limit(clamp_limit(limit))
            )
        )
        .scalars()
        .all()
    )
    return {"items": [payment_view(p) for p in rows]}


@router.post("/payments", status_code=status.HTTP_201_CREATED)
async def add_payment(body: PaymentIn, ctx: EditorDep) -> dict[str, Any]:
    if body.email is None and body.contact_id is None:
        raise ValidationFailed("Say who paid: an email or a contact")
    payment, _ = await record_payment(
        ctx.session,
        ctx.business,
        provider="manual",
        external_id=body.reference or f"manual-{uuid.uuid4().hex[:12]}",
        amount_minor=body.amount_minor,
        currency=(body.currency or ctx.business.currency),
        email=str(body.email) if body.email else None,
        contact_id=body.contact_id,
        kind=body.kind,
        promo_code=body.promo_code,
        description=body.description,
        actor=Actor.user(ctx.user.id),
    )
    return payment_view(payment)
