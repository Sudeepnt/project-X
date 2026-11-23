"""Sequences, newsletter issues and email sender settings."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select

from projectx.api.deps import AdminDep, BusinessDep, EditorDep
from projectx.kernel.errors import NotFound, ValidationFailed
from projectx.modules.gtm.config import gtm_settings, update_gtm_settings
from projectx.modules.gtm.models import Message, NewsletterIssue, Sequence, SequenceEnrollment
from projectx.modules.gtm.nurture.newsletter import issue_view, subscribers
from projectx.modules.gtm.nurture.sequences import render_text
from projectx.spine.jobs.queue import enqueue

router = APIRouter(prefix="/api/v1/b/{business_id}/gtm", tags=["gtm: nurture"])


class Step(BaseModel):
    delay_hours: float = Field(ge=0, le=24 * 60)
    subject: str = Field(default="", max_length=300)
    body: str = Field(min_length=1, max_length=20_000)


class SequenceIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    steps: list[Step] = Field(min_length=1, max_length=20)
    active: bool = True
    channel: Literal["email", "whatsapp"] = "email"
    stop_on: dict[str, Any] = Field(default_factory=dict)


def sequence_view(s: Sequence, stats: dict[str, int] | None = None) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "key": s.key,
        "name": s.name,
        "channel": s.channel,
        "trigger": s.trigger,
        "steps": s.steps,
        "stop_on": s.stop_on,
        "active": s.active,
        "stats": stats or {},
    }


@router.get("/sequences")
async def sequences(ctx: BusinessDep) -> list[dict[str, Any]]:
    rows = (
        (
            await ctx.session.execute(
                select(Sequence).where(Sequence.business_id == ctx.business_id).order_by(Sequence.created_at)
            )
        )
        .scalars()
        .all()
    )
    counts = {
        (sid, st): n
        for sid, st, n in (
            await ctx.session.execute(
                select(SequenceEnrollment.sequence_id, SequenceEnrollment.status, func.count())
                .where(SequenceEnrollment.business_id == ctx.business_id)
                .group_by(SequenceEnrollment.sequence_id, SequenceEnrollment.status)
            )
        ).all()
    }
    sent = dict(
        (
            await ctx.session.execute(
                select(Message.sequence_id, func.count())
                .where(
                    Message.business_id == ctx.business_id, Message.status == "sent", Message.sequence_id.is_not(None)
                )
                .group_by(Message.sequence_id)
            )
        )
        .tuples()
        .all()
    )
    return [
        sequence_view(
            s,
            {
                "active": counts.get((s.id, "active"), 0),
                "completed": counts.get((s.id, "completed"), 0),
                "stopped": counts.get((s.id, "stopped"), 0),
                "sent": int(sent.get(s.id, 0)),
            },
        )
        for s in rows
    ]


@router.put("/sequences/{sequence_id}")
async def update_sequence(sequence_id: uuid.UUID, body: SequenceIn, ctx: EditorDep) -> dict[str, Any]:
    seq = await ctx.session.get(Sequence, sequence_id)
    if seq is None:
        raise NotFound("Sequence not found")
    for step in body.steps:  # fail fast on template errors
        try:
            render_text(step.subject + step.body, {"first_name": "Ada", "business_name": "Test"})
        except Exception as exc:
            raise ValidationFailed(f"Template error: {exc}") from exc
    seq.name, seq.steps, seq.active, seq.channel = (
        body.name,
        [s.model_dump() for s in body.steps],
        body.active,
        body.channel,
    )
    seq.stop_on = body.stop_on
    return sequence_view(seq)


@router.get("/newsletter/issues")
async def issues(ctx: BusinessDep) -> dict[str, Any]:
    rows = (
        (
            await ctx.session.execute(
                select(NewsletterIssue)
                .where(NewsletterIssue.business_id == ctx.business_id)
                .order_by(NewsletterIssue.created_at.desc())
                .limit(60)
            )
        )
        .scalars()
        .all()
    )
    audience = await subscribers(ctx.session, ctx.business_id)
    return {
        "items": [issue_view(i) for i in rows],
        "subscribers": len(audience),
        "settings": gtm_settings(ctx.business)["newsletter"],
    }


@router.post("/newsletter/build", status_code=status.HTTP_202_ACCEPTED)
async def build_now(ctx: EditorDep) -> dict[str, Any]:
    job_id = await enqueue(ctx.session, "gtm.build_newsletter", business_id=ctx.business_id)
    return {"job_id": str(job_id) if job_id else None}


class EmailSettingsIn(BaseModel):
    reply_to_email: EmailStr | None = None
    postal_address: str | None = Field(default=None, max_length=300)
    newsletter_enabled: bool | None = None
    newsletter_send_hour: int | None = Field(default=None, ge=0, le=23)
    double_opt_in: bool | None = None


@router.put("/email-settings")
async def email_settings(body: EmailSettingsIn, ctx: AdminDep) -> dict[str, Any]:
    settings = dict(ctx.business.settings or {})
    if body.reply_to_email is not None:
        settings["reply_to_email"] = str(body.reply_to_email)
    if body.postal_address is not None:
        settings["postal_address"] = body.postal_address
    ctx.business.settings = settings
    newsletter = dict(gtm_settings(ctx.business)["newsletter"])
    if body.newsletter_enabled is not None:
        newsletter["enabled"] = body.newsletter_enabled
    if body.newsletter_send_hour is not None:
        newsletter["send_hour"] = body.newsletter_send_hour
    if body.double_opt_in is not None:
        newsletter["double_opt_in"] = body.double_opt_in
    update_gtm_settings(ctx.business, {"newsletter": newsletter})
    return {
        "reply_to_email": settings.get("reply_to_email"),
        "postal_address": settings.get("postal_address"),
        "newsletter": newsletter,
    }
