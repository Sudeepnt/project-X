"""Our own long-term memory: save with history, recall for a prompt, learn from what people say.

Rules that keep it trustworthy:
- Nothing is stored unless the person's own words support it: every learned entry quotes its evidence,
  and code checks those words are really in what they said (so a model can't invent a memory, or carry
  one over from someone else).
- Secrets never go in: card numbers, passwords, one-time codes and keys are refused by pattern.
- A change never overwrites: the old entry is kept as superseded, so history is complete.
- Each prompt gets only what it needs: pinned entries plus the few most relevant, bounded in size.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.errors import NotFound, ValidationFailed
from projectx.kernel.time import utcnow, zone
from projectx.services import Services
from projectx.spine.context.models import ContextEntry
from projectx.spine.gateway.contracts import ChatMessage, GatewayError, ModelRequest
from projectx.spine.gateway.offline import offline_generator
from projectx.spine.jobs.registry import JobContext, RetryLater, job
from projectx.spine.ledger.events import EventData, register_event
from projectx.spine.ledger.service import Actor, append
from projectx.spine.tenancy.models import Business

KINDS = ("preference", "person", "fact", "routine", "commitment")
PROMPT_ENTRIES = 12
MIN_CONFIDENCE = 0.6
MAX_VALUE = 500


class ContextChanged(EventData):
    entry_id: str
    kind: str
    change: str  # learned | updated | confirmed | forgotten | pinned
    source: str


register_event("spine.context.changed", "The assistant learned, changed or forgot something", ContextChanged)

_SECRETS = (
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),  # card-like numbers
    re.compile(r"\b(?:password|passcode|passwd|pin|otp|cvv|api[_ -]?key|secret|token)\b\s*(?:is|:|=)", re.I),
    re.compile(r"\b(?:sk|pk|rk)_[A-Za-z0-9]{12,}\b"),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
)


def looks_secret(value: str) -> bool:
    return any(p.search(value) for p in _SECRETS)


def normalize_key(key: str) -> str:
    key = re.sub(r"[^a-z0-9._]+", "_", key.strip().lower()).strip("._")
    return re.sub(r"_+", "_", key)[:120]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def supported_by(evidence: str, text: str) -> bool:
    """The quoted words must be in the person's text (case, spacing and punctuation aside)."""
    e = _norm(evidence)
    return bool(e) and len(e) >= 3 and e in _norm(text)


# ---------------------------------------------------------------- save, forget, recall


async def save(
    session: AsyncSession,
    business_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None,
    kind: str,
    key: str,
    value: str,
    source: str,
    evidence: str = "",
    confidence: float = 0.9,
    pinned: bool | None = None,
    data: dict[str, Any] | None = None,
    actor: Actor,
) -> ContextEntry:
    if kind not in KINDS:
        raise ValidationFailed(f"kind is one of {', '.join(KINDS)}")
    value = value.strip()[:MAX_VALUE]
    key = normalize_key(key)
    if not key or not value:
        raise ValidationFailed("Both a key and a value are needed")
    if looks_secret(value) or looks_secret(evidence):
        raise ValidationFailed("That looks like a password, card number or code. Those are never stored here.")
    current = await active(session, business_id, user_id, key, lock=True)
    if current is not None and _norm(current.value) == _norm(value):
        current.confirmed_at = utcnow()
        current.confidence = max(current.confidence, confidence)
        if pinned is not None:
            current.pinned = pinned
        await _log(session, business_id, current, "confirmed", actor)
        return current
    entry = ContextEntry(
        business_id=business_id,
        user_id=user_id,
        kind=kind,
        key=key,
        value=value,
        source=source,
        evidence=evidence[:500],
        confidence=confidence,
        pinned=pinned if pinned is not None else bool(current and current.pinned),
        data=data or {},
        supersedes_id=current.id if current else None,
    )
    if current is not None:
        current.status, current.ended_at = "superseded", utcnow()
        await session.flush()  # free the active key before the new row takes it
    session.add(entry)
    await session.flush()
    await _log(session, business_id, entry, "updated" if current else "learned", actor)
    return entry


async def active(
    session: AsyncSession, business_id: uuid.UUID, user_id: uuid.UUID | None, key: str, *, lock: bool = False
) -> ContextEntry | None:
    stmt = select(ContextEntry).where(
        ContextEntry.business_id == business_id,
        ContextEntry.key == normalize_key(key),
        ContextEntry.status == "active",
        ContextEntry.user_id.is_(None) if user_id is None else ContextEntry.user_id == user_id,
    )
    if lock:
        stmt = stmt.with_for_update()
    found: ContextEntry | None = await session.scalar(stmt)
    return found


async def forget(session: AsyncSession, business_id: uuid.UUID, entry_id: uuid.UUID, *, actor: Actor) -> None:
    """Forget means gone: the value and the words it came from are wiped from every version."""
    entry = await session.get(ContextEntry, entry_id)
    if entry is None:
        raise NotFound("Nothing stored under that id")
    chain = [entry]
    while chain[-1].supersedes_id:
        older = await session.get(ContextEntry, chain[-1].supersedes_id)
        if older is None:
            break
        chain.append(older)
    for e in chain:
        e.value, e.evidence, e.data = "[forgotten]", "", {}
        e.status, e.ended_at = "forgotten", e.ended_at or utcnow()
    await _log(session, business_id, entry, "forgotten", actor)


async def _log(session: AsyncSession, business_id: uuid.UUID, e: ContextEntry, change: str, actor: Actor) -> None:
    await append(
        session,
        business_id=business_id,
        type="spine.context.changed",
        actor=actor,
        subject=("context_entry", e.id),
        data={"entry_id": str(e.id), "kind": e.kind, "change": change, "source": e.source},
    )


async def recall_for(
    session: AsyncSession, business_id: uuid.UUID, user_id: uuid.UUID | None, query: str, *, limit: int = PROMPT_ENTRIES
) -> list[ContextEntry]:
    """Pinned entries first, then the most relevant to `query`, then the freshest. Business-wide and personal."""
    scope = [
        ContextEntry.business_id == business_id,
        ContextEntry.status == "active",
        or_(ContextEntry.user_id.is_(None), ContextEntry.user_id == user_id)
        if user_id
        else ContextEntry.user_id.is_(None),
    ]
    picked: dict[uuid.UUID, ContextEntry] = {}
    for e in (
        await session.execute(select(ContextEntry).where(*scope, ContextEntry.pinned.is_(True)).limit(limit))
    ).scalars():
        picked[e.id] = e
    words = " OR ".join(w for w in re.findall(r"[A-Za-z]{3,}", query)[:12])
    if words and len(picked) < limit:
        tsq = func.websearch_to_tsquery("english", words)
        relevant = await session.execute(
            select(ContextEntry)
            .where(*scope, ContextEntry.search.op("@@")(tsq))
            .order_by(func.ts_rank(ContextEntry.search, tsq).desc(), ContextEntry.confirmed_at.desc())
            .limit(limit)
        )
        for e in relevant.scalars():
            if len(picked) >= limit:
                break
            picked.setdefault(e.id, e)
    if len(picked) < limit:
        fresh = await session.execute(
            select(ContextEntry)
            .where(*scope, ContextEntry.kind.in_(("preference", "commitment", "routine")))
            .order_by(ContextEntry.confirmed_at.desc())
            .limit(limit)
        )
        for e in fresh.scalars():
            if len(picked) >= limit:
                break
            picked.setdefault(e.id, e)
    return list(picked.values())


def as_prompt(entries: list[ContextEntry]) -> str:
    if not entries:
        return ""
    lines = [f"- [{e.kind}] {e.value}" for e in entries]
    return "WHAT YOU KNOW ABOUT THEM (from your own notes; trust it unless they say otherwise):\n" + "\n".join(lines)


def entry_view(e: ContextEntry) -> dict[str, Any]:
    return {
        "id": str(e.id),
        "user_id": str(e.user_id) if e.user_id else None,
        "kind": e.kind,
        "key": e.key,
        "value": e.value,
        "source": e.source,
        "evidence": e.evidence,
        "confidence": e.confidence,
        "pinned": e.pinned,
        "status": e.status,
        "data": e.data,
        "supersedes_id": str(e.supersedes_id) if e.supersedes_id else None,
        "confirmed_at": e.confirmed_at.isoformat() if e.confirmed_at else None,
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "ended_at": e.ended_at.isoformat() if e.ended_at else None,
    }


# ---------------------------------------------------------------- learning from a conversation


class Learned(BaseModel):
    kind: Literal["preference", "person", "fact", "routine", "commitment"]
    key: str = Field(description="Stable dotted key, e.g. 'meetings.no_mondays', 'person.priya', 'routine.gym'")
    value: str = Field(description="One plain sentence in the third person, e.g. 'Prefers no meetings on Mondays'")
    evidence: str = Field(description="The exact words they used that show this")
    confidence: float = Field(description="0 to 1: how sure, from their words alone")
    for_whole_business: bool = Field(description="True only for facts about the company, not this person")
    due: str = Field(description="commitment only: when it is due, YYYY-MM-DD or YYYY-MM-DDTHH:MM local; else empty")


class Learning(BaseModel):
    items: list[Learned] = Field(description="Empty when nothing durable was said")


_LEARN_SYSTEM = """You keep the long-term notes of an executive assistant. Read what the person just said and note only
what will still matter in a month: how they like things done (preferences), people and who they are (person),
stable facts about them or the business (fact), habits (routine), and promises they made to someone (commitment:
"I'll send Priya the deck by Friday"). Today is {today} in {tz}.

- Never note one-off requests, moods, or anything you'd have to guess. Quote their exact words as evidence.
- Never note passwords, card numbers, codes, health details or anything about a third party beyond name and role.
- Reuse an existing key from KNOWN NOTES when the same thing changes, so the note is updated, not duplicated.
- At most 5 notes. Usually there are none."""


async def learn_from(
    services: Services, business_id: uuid.UUID, user_id: uuid.UUID | None, text: str, *, source: str = "assistant"
) -> list[ContextEntry]:
    """Ask an open model what is worth remembering, then keep only what the person's words support."""
    from projectx.spine.calendar import service as cal  # commitments become reminders

    async with services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        assert business is not None
        tz = business.timezone
        known = await recall_for(session, business_id, user_id, text, limit=15)
        known_block = "\n".join(f"- {e.key}: {e.value}" for e in known) or "- (none)"
    today = utcnow().astimezone(zone(tz)).date()
    try:
        result = await services.gateway.run(
            business_id,
            ModelRequest(
                task="spine.context.learn",
                system=_LEARN_SYSTEM.format(today=today.isoformat(), tz=tz),
                messages=[ChatMessage("user", f"KNOWN NOTES:\n{known_block}\n\nTHEY SAID:\n{text[:4000]}")],
                output_schema=Learning,
                max_tokens=1000,
                rung="small",
                max_rung="mid",
                context={"text": text[:4000]},
            ),
        )
    except GatewayError as exc:
        raise RetryLater(300, f"model unavailable: {exc}") from exc
    learning = result.parsed_as(Learning)
    saved: list[ContextEntry] = []
    async with services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        assert business is not None
        for item in learning.items[:5]:
            if item.confidence < MIN_CONFIDENCE or not supported_by(item.evidence, text):
                continue
            if looks_secret(item.value) or looks_secret(item.evidence):
                continue
            data: dict[str, Any] = {}
            if item.kind == "commitment":
                due = _due(item.due, tz)
                if due is None or due <= utcnow():
                    continue
                reminder = await cal.add_reminder(
                    session,
                    business,
                    title=item.value[:200],
                    due_at=due,
                    kind="follow_up",
                    notes=f"You said: “{item.evidence[:300]}”",
                    notify_before_minutes=[1440, 60] if due - utcnow() > timedelta(days=1) else [60],
                    source="agent",
                    dedupe_key=f"commitment:{normalize_key(item.key)}:{due.date().isoformat()}",
                    actor=Actor.agent("context"),
                    created_by=user_id,
                )
                data = {"reminder_id": str(reminder.id), "due_at": due.isoformat()}
            try:
                saved.append(
                    await save(
                        session,
                        business_id,
                        user_id=None if item.for_whole_business else user_id,
                        kind=item.kind,
                        key=item.key,
                        value=item.value,
                        source=source,
                        evidence=item.evidence,
                        confidence=item.confidence,
                        data=data,
                        actor=Actor.agent("context"),
                    )
                )
            except ValidationFailed:
                continue
    return saved


def _due(value: str, tz: str) -> datetime | None:
    value = value.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2})?", value):
        return None
    try:
        moment = datetime.fromisoformat(value if "T" in value else f"{value}T09:00")
    except ValueError:
        return None
    return moment.replace(tzinfo=zone(tz)).astimezone(UTC)


@job("spine.context.learn", max_attempts=4, timeout_s=120)
async def learn_job(ctx: JobContext) -> dict[str, Any]:
    business_id = ctx.require_business()
    user_id = uuid.UUID(ctx.payload["user_id"]) if ctx.payload.get("user_id") else None
    saved = await learn_from(
        ctx.services, business_id, user_id, ctx.payload["text"], source=ctx.payload.get("source", "assistant")
    )
    return {"learned": len(saved)}


# Offline (no model): learn the plainest statements so the loop works end to end.
_PREF = re.compile(r"\b(?:i (?:prefer|like|always|never|don't|do not)|please (?:always|never))\b[^.!?\n]*", re.I)
_PROMISE = re.compile(r"\bi(?:'ll| will) ([^.!?\n]*?) by (\d{4}-\d{2}-\d{2})", re.I)


@offline_generator("spine.context.learn")
def _offline_learn(req: ModelRequest) -> dict[str, Any]:
    text = str(req.context.get("text", ""))
    items: list[dict[str, Any]] = []
    for m in _PREF.finditer(text):
        phrase = m.group(0).strip()
        items.append(
            {
                "kind": "preference",
                "key": "pref." + "_".join(_norm(phrase).split()[1:6]),
                "value": phrase[0].upper() + phrase[1:],
                "evidence": phrase,
                "confidence": 0.9,
                "for_whole_business": False,
                "due": "",
            }
        )
    for m in _PROMISE.finditer(text):
        items.append(
            {
                "kind": "commitment",
                "key": "promise." + "_".join(_norm(m.group(1)).split()[:5]),
                "value": f"Promised to {m.group(1).strip()}",
                "evidence": m.group(0),
                "confidence": 0.9,
                "for_whole_business": False,
                "due": m.group(2),
            }
        )
    return {"items": items[:5]}
