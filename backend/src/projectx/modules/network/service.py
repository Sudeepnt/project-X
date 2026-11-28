"""Network and handshake: the relationships around the business, and assistants that agree things directly.

Partners: suppliers, referrers, investors, advisors… each with an optional "stay in touch every N days" that
turns into a reminder when it lapses.

Handshake (like two people's assistants booking a meeting between themselves): two businesses on this platform
link their assistants with a one-time code. Each side decides what the other may see; today that is only
`availability` (free/busy, never titles, attendees or anything else). When one asks to meet, the system finds the
first slot both calendars have free, and the other business gets an approval card; once accepted, the meeting
goes on both calendars. A request beyond what was granted is refused, logged, and the business is told.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.errors import Forbidden, NotFound, ValidationFailed
from projectx.kernel.ids import token_urlsafe
from projectx.kernel.time import today_in, utcnow, zone
from projectx.modules.network.models import HandshakeInvite, HandshakeLink, HandshakeMeeting, Partner
from projectx.services import Services
from projectx.spine.calendar import service as cal
from projectx.spine.decisions import service as decisions
from projectx.spine.decisions.executors import ExecutionContext, ExecutionResult, executor, undoer
from projectx.spine.ledger.events import EventData, register_event
from projectx.spine.ledger.service import Actor, append
from projectx.spine.notifications.service import notify
from projectx.spine.policy.actions import ActionType, register_action
from projectx.spine.policy.levels import Level
from projectx.spine.tenancy.models import Business

AGENT = Actor.agent("network")
SCOPES = ("availability",)
ACCEPT = register_action(
    ActionType(
        key="network.accept_meeting",
        module="network",
        pack="network.handshake",
        title="Accept a meeting from a linked business",
        description="Put a meeting the other business's assistant proposed on both calendars.",
        default_level=Level.ASK_FIRST,
        max_level=Level.RUN,
        reversible=True,
        undo="Cancel the meeting on both calendars.",
    )
)


class Linked(EventData):
    link_id: str
    peer_business_id: str
    status: str


class Denied(EventData):
    link_id: str
    scope: str


class MeetingChanged(EventData):
    meeting_ref: str
    status: str
    role: str


for _k, _d, _s in (
    ("network.handshake.linked", "Assistants of two businesses were linked", Linked),
    ("network.handshake.revoked", "A handshake link was revoked", Linked),
    ("network.handshake.denied", "A linked business asked for something it wasn't given", Denied),
    ("network.meeting.proposed", "A linked business proposed a meeting", MeetingChanged),
    ("network.meeting.booked", "A meeting with a linked business was booked", MeetingChanged),
    ("network.meeting.cancelled", "A meeting with a linked business was cancelled", MeetingChanged),
):
    register_event(_k, _d, _s)


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


# ---------------------------------------------------------------- linking


async def create_invite(session: AsyncSession, business_id: uuid.UUID, scopes: list[str]) -> str:
    """Returns the one-time code to give the other business (valid 7 days). Only its hash is stored."""
    if not set(scopes) <= set(SCOPES):
        raise ValidationFailed(f"Scopes are {', '.join(SCOPES)}")
    code = "hx_" + token_urlsafe(24)
    session.add(
        HandshakeInvite(
            business_id=business_id, code_hash=_hash(code), scopes=list(scopes), expires_at=utcnow() + timedelta(days=7)
        )
    )
    await session.flush()
    return code


async def accept_invite(
    services: Services, business_id: uuid.UUID, code: str, grant: list[str], actor: Actor
) -> HandshakeLink:
    if not set(grant) <= set(SCOPES):
        raise ValidationFailed(f"Scopes are {', '.join(SCOPES)}")
    async with services.db.session() as session:
        invite = await session.scalar(
            select(HandshakeInvite).where(HandshakeInvite.code_hash == _hash(code)).with_for_update()
        )
        if invite is None or invite.used_at is not None or invite.expires_at < utcnow():
            raise ValidationFailed("That code is invalid or has expired. Ask for a new one.")
        if invite.business_id == business_id:
            raise ValidationFailed("A business can't link to itself")
        invite.used_at = utcnow()
        inviter, inviter_grants = invite.business_id, list(invite.scopes)
        rows = await session.execute(select(Business.id, Business.name).where(Business.id.in_((inviter, business_id))))
        names: dict[uuid.UUID, str] = dict(rows.tuples().all())
    mine = await _link_side(services, business_id, inviter, names[inviter], grant, actor)
    await _link_side(services, inviter, business_id, names[business_id], inviter_grants, AGENT)
    return mine


async def _link_side(
    services: Services, business_id: uuid.UUID, peer: uuid.UUID, peer_name: str, granted: list[str], actor: Actor
) -> HandshakeLink:
    async with services.db.tenant(business_id) as session:
        link = await session.scalar(select(HandshakeLink).where(HandshakeLink.peer_business_id == peer))
        if link is None:
            link = HandshakeLink(
                business_id=business_id, peer_business_id=peer, peer_name=peer_name[:200], granted=granted
            )
            session.add(link)
        else:
            link.granted, link.status = granted, "active"
        await session.flush()
        await append(
            session,
            business_id=business_id,
            type="network.handshake.linked",
            actor=actor,
            subject=("handshake_link", link.id),
            data={"link_id": str(link.id), "peer_business_id": str(peer), "status": "active"},
        )
        session.expunge(link)
        return link


async def revoke(services: Services, business_id: uuid.UUID, link_id: uuid.UUID, actor: Actor) -> None:
    """Either side can end the link; both sides stop at once."""
    async with services.db.tenant(business_id) as session:
        link = await session.get(HandshakeLink, link_id)
        if link is None:
            raise NotFound("Link not found")
        peer = link.peer_business_id
    for side, other in ((business_id, peer), (peer, business_id)):
        async with services.db.tenant(side) as session:
            row = await session.scalar(select(HandshakeLink).where(HandshakeLink.peer_business_id == other))
            if row is not None and row.status != "revoked":
                row.status = "revoked"
                await append(
                    session,
                    business_id=side,
                    type="network.handshake.revoked",
                    actor=actor if side == business_id else AGENT,
                    subject=("handshake_link", row.id),
                    data={"link_id": str(row.id), "peer_business_id": str(other), "status": "revoked"},
                )


async def _peer_allows(services: Services, asker: uuid.UUID, peer: uuid.UUID, scope: str, asker_name: str) -> bool:
    """Does the peer let the asker use `scope`? If not, the peer is told someone tried."""
    async with services.db.tenant(peer) as session:
        row = await session.scalar(select(HandshakeLink).where(HandshakeLink.peer_business_id == asker))
        if row is not None and row.status == "active" and scope in row.granted:
            return True
        if row is not None:
            await append(
                session,
                business_id=peer,
                type="network.handshake.denied",
                actor=AGENT,
                subject=("handshake_link", row.id),
                data={"link_id": str(row.id), "scope": scope},
            )
            await notify(
                session,
                peer,
                kind="network.denied",
                level="warning",
                title=f"{asker_name} asked for your {scope} but you haven't shared it",
                body="Nothing was shared. You can grant it, or revoke the link.",
                link="/network",
            )
        return False


# ---------------------------------------------------------------- meeting each other


async def propose_meeting(
    services: Services,
    business_id: uuid.UUID,
    link_id: uuid.UUID,
    *,
    title: str,
    duration_minutes: int,
    days: int,
    actor: Actor,
) -> dict[str, Any]:
    async with services.db.tenant(business_id) as session:
        link = await session.get(HandshakeLink, link_id)
        me = await session.get(Business, business_id)
        if link is None or link.status != "active" or me is None:
            raise NotFound("No active link")
        peer, my_name, peer_name = link.peer_business_id, me.name, link.peer_name
    if not await _peer_allows(services, business_id, peer, "availability", my_name):
        raise Forbidden(f"{peer_name} hasn't shared their availability with you.")
    start = utcnow() + timedelta(hours=12)
    end = start + timedelta(days=max(1, min(days, 21)))
    async with services.db.tenant(business_id) as session:
        biz = await session.get(Business, business_id)
        assert biz is not None
        mine = await cal.find_free_time(
            session, biz, start=start, end=end, duration_minutes=duration_minutes, limit=200
        )
    async with services.db.tenant(peer) as session:
        other = await session.get(Business, peer)
        assert other is not None
        theirs = await cal.find_free_time(
            session, other, start=start, end=end, duration_minutes=duration_minutes, limit=200
        )
    both = sorted({(s.start, s.end) for s in mine} & {(s.start, s.end) for s in theirs})
    if not both:
        return {"status": "no_common_time", "message": f"No common free time with {peer_name} in the next {days} days."}
    slot_start, slot_end = both[0]
    ref = uuid.uuid4()
    async with services.db.tenant(business_id) as session:
        session.add(
            HandshakeMeeting(
                business_id=business_id,
                ref=ref,
                link_id=link_id,
                role="asked",
                title=title[:200],
                starts_at=slot_start,
                ends_at=slot_end,
            )
        )
        await append(
            session,
            business_id=business_id,
            type="network.meeting.proposed",
            actor=actor,
            data={"meeting_ref": str(ref), "status": "proposed", "role": "asked"},
        )
    async with services.db.tenant(peer) as session:
        their_link = await session.scalar(select(HandshakeLink).where(HandshakeLink.peer_business_id == business_id))
        assert their_link is not None
        other = await session.get(Business, peer)
        assert other is not None
        row = HandshakeMeeting(
            business_id=peer,
            ref=ref,
            link_id=their_link.id,
            role="invited",
            title=title[:200],
            starts_at=slot_start,
            ends_at=slot_end,
        )
        session.add(row)
        await session.flush()
        when = slot_start.astimezone(zone(other.timezone)).strftime("%a %d %b, %H:%M")
        outcome = await decisions.propose(
            session,
            peer,
            decisions.Proposal(
                action_type=ACCEPT.key,
                title=f"{my_name} wants to meet: “{title[:120]}”, {when}",
                summary=f"The first time you're both free, {duration_minutes} minutes.",
                payload={"meeting_ref": str(ref), "peer_business_id": str(business_id)},
                subject=("handshake_meeting", row.id),
                requested_by="pack:network.handshake",
                priority=30,
                expires_at=slot_start,
            ),
        )
        row.decision_id = outcome.decision_id
        await append(
            session,
            business_id=peer,
            type="network.meeting.proposed",
            actor=AGENT,
            data={"meeting_ref": str(ref), "status": "proposed", "role": "invited"},
        )
    return {
        "status": "proposed",
        "meeting_ref": str(ref),
        "starts_at": slot_start.isoformat(),
        "ends_at": slot_end.isoformat(),
    }


@executor(ACCEPT.key)
async def _accept(ctx: ExecutionContext) -> ExecutionResult:
    """Runs for the invited business: book on both calendars, or report that the time was taken meanwhile."""
    ref, asker = uuid.UUID(ctx.payload["meeting_ref"]), uuid.UUID(ctx.payload["peer_business_id"])
    booked: dict[uuid.UUID, uuid.UUID] = {}
    for side in (ctx.business_id, asker):  # check both calendars first: book on both or on neither
        async with ctx.services.db.tenant(side) as session:
            row = await session.scalar(select(HandshakeMeeting).where(HandshakeMeeting.ref == ref))
            if row is None:
                raise NotFound("Meeting not found")
            busy = await cal.busy_intervals(session, side, row.starts_at, row.ends_at)
            if any(b.start < row.ends_at and row.starts_at < b.end for b in busy):
                raise ValidationFailed("That time is no longer free on one of the calendars; ask for a new time.")
    for side, other in ((ctx.business_id, asker), (asker, ctx.business_id)):
        async with ctx.services.db.tenant(side) as session:
            row = await session.scalar(select(HandshakeMeeting).where(HandshakeMeeting.ref == ref).with_for_update())
            link = await session.scalar(select(HandshakeLink).where(HandshakeLink.peer_business_id == other))
            biz = await session.get(Business, side)
            if row is None or link is None or link.status != "active" or biz is None:
                raise ValidationFailed("The link was revoked; nothing was booked.")
            event, _ = await cal.schedule_event(
                session,
                biz,
                title=f"{row.title} (with {link.peer_name})"[:200],
                starts_at=row.starts_at,
                ends_at=row.ends_at,
                description="Agreed by both assistants through a handshake.",
                actor=AGENT,
            )
            row.status, row.event_id = "booked", event.id
            booked[side] = event.id
            await append(
                session,
                business_id=side,
                type="network.meeting.booked",
                actor=AGENT,
                data={"meeting_ref": str(ref), "status": "booked", "role": row.role},
            )
    return ExecutionResult(
        data={"meeting_ref": str(ref)},
        undo_payload={"meeting_ref": str(ref), "peer_business_id": str(asker)},
        summary="Booked on both calendars.",
    )


@undoer(ACCEPT.key)
async def _cancel(ctx: ExecutionContext) -> dict[str, Any]:
    ref, asker = uuid.UUID(ctx.payload["meeting_ref"]), uuid.UUID(ctx.payload["peer_business_id"])
    for side in (ctx.business_id, asker):
        async with ctx.services.db.tenant(side) as session:
            row = await session.scalar(select(HandshakeMeeting).where(HandshakeMeeting.ref == ref).with_for_update())
            biz = await session.get(Business, side)
            if row is None or biz is None:
                continue
            if row.event_id:
                await cal.cancel_event(session, biz, row.event_id, actor=AGENT)
            row.status = "cancelled"
            await append(
                session,
                business_id=side,
                type="network.meeting.cancelled",
                actor=AGENT,
                data={"meeting_ref": str(ref), "status": "cancelled", "role": row.role},
            )
    return {"cancelled": str(ref)}


# ---------------------------------------------------------------- staying in touch


async def daily(services: Services, business_id: uuid.UUID) -> None:
    async with services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        if business is None:
            return
        today = today_in(business.timezone)
        for p in (
            await session.execute(select(Partner).where(Partner.status == "active", Partner.cadence_days.is_not(None)))
        ).scalars():
            assert p.cadence_days is not None
            last = (p.last_contact_at or p.created_at).astimezone(zone(business.timezone)).date()
            due = last + timedelta(days=p.cadence_days)
            if due <= today + timedelta(days=1):
                await cal.add_reminder(
                    session,
                    business,
                    title=f"Catch up with {p.name}"[:200],
                    due_at=datetime.combine(max(due, today), time(10), tzinfo=zone(business.timezone)),
                    kind="follow_up",
                    notes=p.notes[:500],
                    notify_before_minutes=[0],
                    source="agent",
                    dedupe_key=f"touch:{p.id}:{due.isoformat()}",
                    actor=AGENT,
                )


def partner_view(p: Partner) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "name": p.name,
        "kind": p.kind,
        "contact_id": str(p.contact_id) if p.contact_id else None,
        "cadence_days": p.cadence_days,
        "last_contact_at": p.last_contact_at.isoformat() if p.last_contact_at else None,
        "notes": p.notes,
        "status": p.status,
    }


def link_view(link: HandshakeLink) -> dict[str, Any]:
    return {
        "id": str(link.id),
        "peer_business_id": str(link.peer_business_id),
        "peer_name": link.peer_name,
        "granted": link.granted,
        "status": link.status,
    }
