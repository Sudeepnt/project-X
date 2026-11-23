"""Sending: sequence steps on schedule, and individual messages with a final consent check."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from projectx.kernel.time import utcnow
from projectx.modules.gtm.crm.lifecycle import has_consent
from projectx.modules.gtm.models import Message, Sequence, SequenceEnrollment
from projectx.modules.gtm.nurture.sequences import queue_message, should_stop
from projectx.spine.decisions.executors import ExecutionContext, ExecutionResult, executor
from projectx.spine.decisions.service import Proposal, assess, propose
from projectx.spine.jobs.queue import enqueue
from projectx.spine.jobs.registry import JobContext, PermanentFailure, job, periodic
from projectx.spine.ledger.service import Actor, append
from projectx.spine.policy import autonomy
from projectx.spine.policy.service import get_policy
from projectx.spine.tenancy.models import Business
from projectx.spine.twin.models import Contact

if TYPE_CHECKING:
    from projectx.services import Services


@periodic("gtm.schedule_sequences", every=timedelta(minutes=5))
async def schedule_sequences(services: Services, now: datetime) -> None:
    window = now.strftime("%Y%m%d%H") + f"{now.minute // 5:02d}"
    async with services.db.session() as session:
        ids = (await session.execute(select(Business.id).where(Business.status == "active"))).scalars().all()
        for business_id in ids:
            await enqueue(
                session, "gtm.process_sequences", business_id=business_id, idempotency_key=f"seq:{business_id}:{window}"
            )


@job("gtm.process_sequences", max_attempts=3, timeout_s=600, priority=50)
async def process_sequences(ctx: JobContext) -> dict[str, Any] | None:
    business_id = ctx.require_business()
    sent = waiting = stopped = 0
    async with ctx.services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        if business is None:
            return None
        rows = (
            await session.execute(
                select(SequenceEnrollment, Sequence, Contact)
                .join(Sequence, Sequence.id == SequenceEnrollment.sequence_id)
                .join(Contact, Contact.id == SequenceEnrollment.contact_id)
                .where(
                    SequenceEnrollment.business_id == business_id,
                    SequenceEnrollment.status == "active",
                    SequenceEnrollment.next_run_at <= utcnow(),
                )
                .order_by(SequenceEnrollment.next_run_at)
                .limit(200)
                .with_for_update(of=SequenceEnrollment, skip_locked=True)
            )
        ).all()
        for enrollment, seq, contact in rows:
            reason = should_stop(seq, contact)
            if reason is None and not await has_consent(session, contact, seq.channel):
                reason = "no consent"
            if reason or enrollment.step_index >= len(seq.steps):
                enrollment.status = "stopped" if reason else "completed"
                enrollment.stopped_reason = reason
                stopped += 1
                continue
            step = seq.steps[enrollment.step_index]
            message = await queue_message(
                session,
                ctx.services,
                business,
                contact,
                channel=seq.channel,
                kind="sequence",
                subject=step.get("subject", ""),
                body=step.get("body", ""),
                sequence_id=seq.id,
                enrollment_id=enrollment.id,
            )
            proposal = Proposal(
                action_type="gtm.send_sequence_step",
                title=f"{seq.name}: step {enrollment.step_index + 1}",
                summary=message.subject or message.body_text[:140],
                payload={"message_id": str(message.id)},
                preview={"channel": seq.channel, "subject": message.subject, "text": message.body_text},
                subject=("message", message.id),
                requested_by="pack:gtm.nurture",
            )
            verdict = await assess(session, business_id, proposal)
            if verdict.runs_now:
                await enqueue(
                    session,
                    "gtm.send_message",
                    {"message_id": str(message.id), "auto": True},
                    business_id=business_id,
                    idempotency_key=f"msg:{message.id}",
                )
                sent += 1
            elif verdict.blocked:
                message.status = "suppressed"
                message.error = verdict.explain()
            else:
                await propose(session, business_id, proposal)
                waiting += 1
            enrollment.step_index += 1
            if enrollment.step_index >= len(seq.steps):
                enrollment.status, enrollment.next_run_at = "completed", None
            else:
                delay = float(seq.steps[enrollment.step_index].get("delay_hours", 24))
                enrollment.next_run_at = utcnow() + timedelta(hours=delay)
    return {"sent": sent, "waiting": waiting, "stopped": stopped}


@executor("gtm.send_sequence_step")
async def _approved_sequence_step(ctx: ExecutionContext) -> ExecutionResult:
    message_id = ctx.payload["message_id"]
    async with ctx.services.db.tenant(ctx.business_id) as session:
        await enqueue(
            session,
            "gtm.send_message",
            {"message_id": message_id},
            business_id=ctx.business_id,
            idempotency_key=f"msg:{message_id}",
        )
    return ExecutionResult(data={"message_id": message_id}, undo_payload={"message_id": message_id})


@job("gtm.send_message", max_attempts=6, timeout_s=120, priority=30)
async def send_message(ctx: JobContext) -> dict[str, Any] | None:
    business_id = ctx.require_business()
    message_id = uuid.UUID(ctx.payload["message_id"])
    async with ctx.services.db.tenant(business_id) as session:
        message = await session.get(Message, message_id)
        if message is None:
            raise PermanentFailure("message not found")
        if message.status != "queued":
            return {"skipped": message.status}
        contact = await session.get(Contact, message.contact_id) if message.contact_id else None
        business = await session.get(Business, business_id)
        if contact is None or business is None or not await has_consent(session, contact, message.channel):
            message.status, message.error = "suppressed", "no consent at send time"
            return {"suppressed": True}
        to_email, channel = contact.email, message.channel
        subject, html, text = message.subject or "", message.body_html or "", message.body_text
        reply_to = (business.settings or {}).get("reply_to_email")
        from_addr = f"{business.name} <{ctx.services.settings.email_from.split('<')[-1].rstrip('>')}>"
    if channel != "email" or not to_email:
        raise PermanentFailure(f"channel {channel} not available for this contact")
    from projectx.modules.gtm.nurture.sequences import build_email  # headers for one-click unsubscribe

    headers = build_email(ctx.services, business, contact, subject="", body="")["headers"]
    provider_id = await ctx.services.mailer.send(
        to=to_email, subject=subject, html=html, text=text, from_addr=from_addr, reply_to=reply_to, headers=headers
    )
    async with ctx.services.db.tenant(business_id) as session:
        message = await session.get(Message, message_id)
        assert message is not None
        message.status, message.provider_id, message.sent_at = "sent", provider_id, utcnow()
        await append(
            session,
            business_id=business_id,
            type="gtm.message.sent",
            actor=Actor.agent("pack:gtm.nurture"),
            subject=("message", message.id),
            data={
                "message_id": str(message.id),
                "channel": message.channel,
                "kind": message.kind,
                "sequence_id": str(message.sequence_id) if message.sequence_id else None,
                "issue_id": str(message.issue_id) if message.issue_id else None,
            },
        )
        if ctx.payload.get("auto") and message.kind == "sequence":
            policy = await get_policy(session, business_id)
            await autonomy.record_outcome(
                session, business_id, "gtm.send_sequence_step", "clean", threshold=policy.promotion_threshold
            )
    return {"sent": str(message_id)}
