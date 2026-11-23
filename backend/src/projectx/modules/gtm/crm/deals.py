"""Deal pipelines. Contacts carry the lifecycle stage; deals carry B2B opportunities (HubSpot's
proven split). Bought's advertiser funnel is a deal pipeline: Prospect → Contacted → Meeting →
Proposal → Advertiser, then renewals and upsells as new deals."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.errors import NotFound, ValidationFailed
from projectx.kernel.time import utcnow
from projectx.modules.gtm.crm.lifecycle import Cause, change
from projectx.spine.ledger.service import Actor, append
from projectx.spine.twin.models import Contact, Deal, Pipeline

PIPELINE_TEMPLATES: dict[str, dict[str, Any]] = {
    "sales": {
        "name": "Sales",
        "stages": [
            {"key": "qualified", "label": "Qualified", "probability": 0.2, "kind": "open", "contact_stage": "sql"},
            {"key": "meeting", "label": "Meeting", "probability": 0.4, "kind": "open", "contact_stage": "opportunity"},
            {
                "key": "proposal",
                "label": "Proposal sent",
                "probability": 0.6,
                "kind": "open",
                "contact_stage": "opportunity",
            },
            {"key": "won", "label": "Won", "probability": 1.0, "kind": "won", "contact_stage": "customer"},
            {"key": "lost", "label": "Lost", "probability": 0.0, "kind": "lost"},
        ],
    },
    "advertisers": {
        "name": "Advertisers",
        "stages": [
            {"key": "prospect", "label": "Prospect", "probability": 0.05, "kind": "open", "contact_stage": "lead"},
            {"key": "contacted", "label": "Contacted", "probability": 0.15, "kind": "open", "contact_stage": "lead"},
            {"key": "meeting", "label": "Meeting", "probability": 0.35, "kind": "open", "contact_stage": "sql"},
            {
                "key": "proposal",
                "label": "Proposal",
                "probability": 0.6,
                "kind": "open",
                "contact_stage": "opportunity",
            },
            {
                "key": "advertiser",
                "label": "Advertiser",
                "probability": 1.0,
                "kind": "won",
                "contact_stage": "customer",
            },
            {"key": "lost", "label": "Lost", "probability": 0.0, "kind": "lost"},
        ],
    },
    "partners": {
        "name": "Partners",
        "stages": [
            {"key": "identified", "label": "Identified", "probability": 0.05, "kind": "open"},
            {"key": "pitched", "label": "Pitched", "probability": 0.2, "kind": "open"},
            {"key": "negotiating", "label": "Negotiating", "probability": 0.5, "kind": "open"},
            {"key": "signed", "label": "Signed", "probability": 1.0, "kind": "won"},
            {"key": "lost", "label": "Lost", "probability": 0.0, "kind": "lost"},
        ],
    },
}

_STATUS_FOR_STAGE = {"contacted": "contacted", "meeting": "meeting_scheduled", "proposal": "proposal_sent"}


async def ensure_pipeline(
    session: AsyncSession, business_id: uuid.UUID, key: str, *, default: bool = False
) -> Pipeline:
    existing = await session.scalar(select(Pipeline).where(Pipeline.business_id == business_id, Pipeline.key == key))
    if existing is not None:
        return existing
    template = PIPELINE_TEMPLATES[key]
    pipeline = Pipeline(
        business_id=business_id, key=key, name=template["name"], stages=template["stages"], is_default=default
    )
    session.add(pipeline)
    await session.flush()
    return pipeline


def _stage(pipeline: Pipeline, key: str) -> dict[str, Any]:
    for stage in pipeline.stages:
        if stage["key"] == key:
            return dict(stage)
    raise ValidationFailed(f"Unknown stage {key!r} for pipeline {pipeline.name}")


async def create_deal(
    session: AsyncSession,
    *,
    business_id: uuid.UUID,
    pipeline: Pipeline,
    title: str,
    contact: Contact | None,
    value_minor: int,
    currency: str,
    stage_key: str | None = None,
    deal_type: str = "one_time",
    expected_close: date | None = None,
    campaign_id: uuid.UUID | None = None,
    actor: Actor,
) -> Deal:
    stage = _stage(pipeline, stage_key or pipeline.stages[0]["key"])
    deal = Deal(
        business_id=business_id,
        pipeline_id=pipeline.id,
        contact_id=contact.id if contact else None,
        title=title[:200],
        stage_key=stage["key"],
        status="open",
        value_minor=value_minor,
        currency=currency,
        deal_type=deal_type,
        expected_close=expected_close,
        campaign_id=campaign_id,
        attributes={},
    )
    session.add(deal)
    await session.flush()
    await _after_move(session, deal, pipeline, stage, contact, actor)
    return deal


async def move_deal(
    session: AsyncSession, deal: Deal, *, stage_key: str, actor: Actor, lost_reason: str | None = None
) -> Deal:
    pipeline = await session.get(Pipeline, deal.pipeline_id)
    if pipeline is None:
        raise NotFound("Pipeline not found")
    stage = _stage(pipeline, stage_key)
    if stage["kind"] == "lost" and not lost_reason:
        raise ValidationFailed("Record why the deal was lost; it is what the loop learns from.")
    deal.stage_key = stage["key"]
    deal.stage_entered_at = utcnow()
    if stage["kind"] == "won":
        deal.status, deal.won_at = "won", utcnow()
    elif stage["kind"] == "lost":
        deal.status, deal.lost_at, deal.lost_reason = "lost", utcnow(), lost_reason
    else:
        deal.status = "open"
    contact = await session.get(Contact, deal.contact_id) if deal.contact_id else None
    await _after_move(session, deal, pipeline, stage, contact, actor)
    return deal


async def _after_move(
    session: AsyncSession, deal: Deal, pipeline: Pipeline, stage: dict[str, Any], contact: Contact | None, actor: Actor
) -> None:
    await append(
        session,
        business_id=deal.business_id,
        type="gtm.deal.changed",
        actor=actor,
        subject=("deal", deal.id),
        data={
            "deal_id": str(deal.id),
            "pipeline": pipeline.key,
            "stage": deal.stage_key,
            "status": deal.status,
            "value_minor": deal.value_minor,
            "currency": deal.currency,
        },
    )
    if contact is None:
        return
    cause = Cause("deal", deal.id, actor=actor)
    if stage["kind"] == "won":
        contact.customer_type = "retainer" if deal.deal_type == "retainer" else contact.customer_type or deal.deal_type
        await change(session, contact, cause=cause, stage=stage.get("contact_stage", "customer"), status="won")
    elif stage["kind"] == "lost":
        await change(session, contact, cause=cause, status="lost", reason=deal.lost_reason)
    else:
        await change(
            session, contact, cause=cause, stage=stage.get("contact_stage"), status=_STATUS_FOR_STAGE.get(stage["key"])
        )


def deal_view(d: Deal) -> dict[str, Any]:
    return {
        "id": str(d.id),
        "pipeline_id": str(d.pipeline_id),
        "contact_id": str(d.contact_id) if d.contact_id else None,
        "title": d.title,
        "stage_key": d.stage_key,
        "status": d.status,
        "value_minor": d.value_minor,
        "currency": d.currency,
        "deal_type": d.deal_type,
        "expected_close": d.expected_close.isoformat() if d.expected_close else None,
        "won_at": d.won_at.isoformat() if d.won_at else None,
        "lost_reason": d.lost_reason,
        "campaign_id": str(d.campaign_id) if d.campaign_id else None,
        "next_action": d.next_action,
        "next_action_at": d.next_action_at.isoformat() if d.next_action_at else None,
        "notes": d.notes,
        "stage_entered_at": d.stage_entered_at.isoformat(),
        "created_at": d.created_at.isoformat(),
    }


def pipeline_view(p: Pipeline) -> dict[str, Any]:
    return {"id": str(p.id), "key": p.key, "name": p.name, "stages": p.stages, "is_default": p.is_default}
