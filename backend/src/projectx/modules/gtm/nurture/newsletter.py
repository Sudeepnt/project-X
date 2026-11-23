"""The newsletter: today's approved newsletter blurbs become an issue (for Bought, the daily
digest), which goes through the policy engine and is sent only to people who opted in."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from markupsafe import Markup
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.errors import NotFound
from projectx.kernel.time import today_in, utcnow
from projectx.modules.gtm.config import gtm_settings
from projectx.modules.gtm.models import ContentItem, ContentVariant, Message, NewsletterIssue
from projectx.modules.gtm.nurture.sequences import build_email
from projectx.services import Services
from projectx.spine.billing.plans import add_usage
from projectx.spine.billing.plans import remaining as plan_remaining
from projectx.spine.decisions.executors import ExecutionContext, ExecutionResult, executor, undoer
from projectx.spine.decisions.service import Proposal, propose
from projectx.spine.jobs.queue import enqueue
from projectx.spine.jobs.registry import JobContext, job
from projectx.spine.ledger.service import Actor, append
from projectx.spine.notifications.service import notify
from projectx.spine.tenancy.models import Business
from projectx.spine.twin.models import Consent, Contact

AGENT = Actor.agent("pack:gtm.newsletter")
BATCH = 200


def render_sections(sections: list[dict[str, Any]]) -> tuple[str, str]:
    html_parts, text_parts = [], []
    for s in sections:
        title, body, url = s.get("title", ""), s.get("text", ""), s.get("url")
        link = Markup('<p><a href="{}">Read more</a></p>').format(url) if url else Markup("")
        html_parts.append(
            Markup('<h2 style="font-size:19px;margin:24px 0 8px">{}</h2><p>{}</p>{}').format(title, body, link)
        )
        text_parts.append(f"{title}\n{body}\n{url or ''}".strip())
    return str(Markup("").join(html_parts)), "\n\n".join(text_parts)


async def subscribers(session: AsyncSession, business_id: uuid.UUID) -> list[Contact]:
    """Contacts whose latest email consent is 'granted' and who can still be emailed."""
    rows = (
        (
            await session.execute(
                select(Contact).where(
                    Contact.business_id == business_id,
                    Contact.email.is_not(None),
                    Contact.unsubscribed_at.is_(None),
                    Contact.erased_at.is_(None),
                    or_(Contact.side_state.is_(None), Contact.side_state != "do_not_contact"),
                    Contact.email_status.notin_(("bounced", "complained")),
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return []
    latest: dict[uuid.UUID, str] = {}
    for contact_id, consent_status in (
        await session.execute(
            select(Consent.contact_id, Consent.status)
            .where(
                Consent.business_id == business_id,
                Consent.channel == "email",
                Consent.contact_id.in_([c.id for c in rows]),
            )
            .order_by(Consent.recorded_at)
        )
    ).all():
        latest[contact_id] = consent_status
    return [c for c in rows if latest.get(c.id) == "granted"]


@job("gtm.build_newsletter", max_attempts=3, timeout_s=300, priority=60)
async def build_newsletter(ctx: JobContext) -> dict[str, Any] | None:
    business_id = ctx.require_business()
    async with ctx.services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        if business is None:
            return None
        variants = (
            await session.execute(
                select(ContentVariant, ContentItem)
                .join(ContentItem, ContentItem.id == ContentVariant.content_id)
                .where(
                    ContentVariant.business_id == business_id,
                    ContentVariant.platform == "newsletter",
                    ContentVariant.status == "ready",
                    ContentItem.status.notin_(("rejected", "archived")),
                )
                .order_by(ContentItem.planned_for)
                .limit(12)
            )
        ).all()
        if not variants:
            return {"built": False}
        sections = [
            {
                "content_id": str(item.id),
                "variant_id": str(v.id),
                "title": v.title or item.title,
                "text": v.text,
                "url": (item.source_ref or {}).get("url"),
            }
            for v, item in variants
        ]
        html, text = render_sections(sections)
        day = today_in(business.timezone)
        title = f"{business.name} · {day:%A %d %B}"
        issue = NewsletterIssue(
            business_id=business_id,
            title=title,
            subject=sections[0]["title"][:300],
            preheader=f"{len(sections)} stories today",
            sections=sections,
            body_html=html,
            body_text=text,
            status="draft",
            scheduled_at=utcnow() + timedelta(hours=1),
            content_ids=[s["content_id"] for s in sections],
        )
        session.add(issue)
        await session.flush()
        for v, _ in variants:
            v.status = "scheduled"
        audience = await subscribers(session, business_id)
        outcome = await propose(
            session,
            business_id,
            Proposal(
                action_type="gtm.send_newsletter",
                title=f"Send today's newsletter to {len(audience):,} subscribers",
                summary=issue.subject,
                payload={"issue_id": str(issue.id)},
                preview={"subject": issue.subject, "sections": sections},
                subject=("newsletter_issue", issue.id),
                why_now="Daily issue, built from today's approved stories.",
                if_no="No newsletter goes out today.",
                requested_by="pack:gtm.newsletter",
                priority=30,
                expires_at=utcnow() + timedelta(hours=18),
            ),
        )
        issue.decision_id = outcome.decision_id
    return {"built": True, "issue_id": str(issue.id), "status": outcome.status}


@executor("gtm.send_newsletter")
async def send_issue(ctx: ExecutionContext) -> ExecutionResult:
    issue_id = uuid.UUID(ctx.payload["issue_id"])
    async with ctx.services.db.tenant(ctx.business_id) as session:
        issue = await session.get(NewsletterIssue, issue_id)
        business = await session.get(Business, ctx.business_id)
        if issue is None or business is None:
            raise NotFound("Issue not found")
        if issue.status in ("sending", "sent"):
            return ExecutionResult(data={"skipped": issue.status})
        audience = await subscribers(session, ctx.business_id)
        for contact in audience:
            built = build_email(
                ctx.services, business, contact, subject=issue.subject, body=issue.body_text, preheader=issue.preheader
            )
            session.add(
                Message(
                    business_id=ctx.business_id,
                    contact_id=contact.id,
                    channel="email",
                    kind="newsletter",
                    issue_id=issue.id,
                    subject=built["subject"],
                    body_text=built["text"],
                    body_html=_issue_html(ctx.services, business, contact, issue),
                    status="queued",
                )
            )
        issue.status, issue.stats = "sending", {"recipients": len(audience)}
        await session.flush()
        await enqueue(
            session,
            "gtm.send_issue_batch",
            {"issue_id": str(issue.id)},
            business_id=ctx.business_id,
            idempotency_key=f"issue:{issue.id}",
        )
    return ExecutionResult(
        data={"recipients": len(audience)},
        summary=f"Sending to {len(audience)} subscribers",
        undo_payload={"issue_id": str(issue_id)},
    )


def _issue_html(services: Services, business: Business, contact: Contact, issue: NewsletterIssue) -> str:
    from projectx.modules.gtm.nurture.sequences import template_context
    from projectx.spine.mail import render

    ctx = template_context(services, business, contact)
    address = (business.settings or {}).get("postal_address", "")
    return render(
        "email/business.html",
        business_name=business.name,
        preheader=issue.preheader,
        body_html=Markup(issue.body_html),  # noqa: S704 - built by render_sections, which escapes every field
        unsubscribe_url=ctx["unsubscribe_url"],
        postal_address=address,
    )


@undoer("gtm.send_newsletter")
async def unpublish_issue(ctx: ExecutionContext) -> dict[str, Any]:
    async with ctx.services.db.tenant(ctx.business_id) as session:
        issue = await session.get(NewsletterIssue, uuid.UUID((ctx.undo_payload or {})["issue_id"]))
        if issue is not None:
            issue.audience = {**issue.audience, "archived_hidden": True}
            for m in (
                await session.execute(select(Message).where(Message.issue_id == issue.id, Message.status == "queued"))
            ).scalars():
                m.status = "cancelled"
    return {"archive_hidden": True, "note": "Emails already delivered can't be recalled."}


@job("gtm.send_issue_batch", max_attempts=8, timeout_s=900, priority=30)
async def send_issue_batch(ctx: JobContext) -> dict[str, Any] | None:
    business_id = ctx.require_business()
    issue_id = uuid.UUID(ctx.payload["issue_id"])
    async with ctx.services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        queued = (
            await session.execute(
                select(Message, Contact)
                .join(Contact, Contact.id == Message.contact_id)
                .where(Message.issue_id == issue_id, Message.status == "queued")
                .limit(BATCH)
            )
        ).all()
        room = await plan_remaining(session, business, "emails_per_month") if business else None
        if room is not None and room < len(queued):
            for m, _ in queued[room:]:  # over the plan's monthly email limit: don't send, say why
                m.status, m.error = "suppressed", "monthly email limit reached on the plan"
            queued = queued[:room]
            await notify(
                session,
                business_id,
                kind="billing.limit_reached",
                level="warning",
                title="Monthly email limit reached",
                body="Some subscribers didn't get this issue. Upgrade to send to everyone.",
                link="/settings?tab=plan",
            )
        jobs = [(m.id, c.email, m.subject, m.body_html, m.body_text) for m, c in queued]
        reply_to = (business.settings or {}).get("reply_to_email") if business else None
        from_name = business.name if business else "Newsletter"
        headers_for = {m.id: _headers(ctx.services, business, c) for m, c in queued} if business else {}
    sent = 0
    for message_id, email, subject, html, text in jobs:
        if not email:
            continue
        provider_id = await ctx.services.mailer.send(
            to=email,
            subject=subject or "",
            html=html or "",
            text=text,
            reply_to=reply_to,
            from_addr=f"{from_name} <{ctx.services.settings.email_from.split('<')[-1].rstrip('>')}>",
            headers=headers_for.get(message_id),
        )
        async with ctx.services.db.tenant(business_id) as session:
            message = await session.get(Message, message_id)
            if message is not None:
                message.status, message.provider_id, message.sent_at = "sent", provider_id, utcnow()
                await add_usage(session, business_id, "emails_per_month")
        sent += 1
    async with ctx.services.db.tenant(business_id) as session:
        remaining = await session.scalar(
            select(Message.id).where(Message.issue_id == issue_id, Message.status == "queued").limit(1)
        )
        issue = await session.get(NewsletterIssue, issue_id)
        if remaining is not None:
            await enqueue(
                session,
                "gtm.send_issue_batch",
                {"issue_id": str(issue_id)},
                business_id=business_id,
                run_at=utcnow() + timedelta(seconds=5),
            )
        elif issue is not None and issue.status != "sent":
            issue.status, issue.sent_at = "sent", utcnow()
            total = int((issue.stats or {}).get("recipients", 0))
            await append(
                session,
                business_id=business_id,
                type="gtm.newsletter.sent",
                actor=AGENT,
                subject=("newsletter_issue", issue.id),
                data={"issue_id": str(issue.id), "recipients": total},
            )
            for content_id in issue.content_ids:
                item = await session.get(ContentItem, uuid.UUID(content_id))
                if item is not None and item.status not in ("published",):
                    item.status = "published"
    return {"sent": sent}


def _headers(services: Services, business: Business, contact: Contact) -> dict[str, str]:
    from projectx.modules.gtm.nurture.sequences import template_context

    url = template_context(services, business, contact)["unsubscribe_url"]
    return {"List-Unsubscribe": f"<{url}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}


def issue_view(i: NewsletterIssue) -> dict[str, Any]:
    return {
        "id": str(i.id),
        "title": i.title,
        "subject": i.subject,
        "preheader": i.preheader,
        "sections": i.sections,
        "status": i.status,
        "scheduled_at": i.scheduled_at.isoformat() if i.scheduled_at else None,
        "sent_at": i.sent_at.isoformat() if i.sent_at else None,
        "stats": i.stats,
        "decision_id": str(i.decision_id) if i.decision_id else None,
        "created_at": i.created_at.isoformat(),
    }


def newsletter_enabled(business: Business) -> bool:
    return bool(gtm_settings(business)["newsletter"].get("enabled"))
