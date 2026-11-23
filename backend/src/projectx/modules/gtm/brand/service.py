"""The brand book: drafted from the business's website and socials, approved by the owner,
versioned. Every post, ad and reply is written from the approved version and checked against it."""

from __future__ import annotations

import re
import uuid
from typing import Any

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.errors import Conflict, NotFound
from projectx.kernel.time import utcnow
from projectx.modules.gtm.brand.site import SiteSummary, read_site
from projectx.modules.gtm.models import Audience, BrandBook
from projectx.services import Services
from projectx.spine.decisions.executors import ExecutionContext, ExecutionResult, executor
from projectx.spine.decisions.service import Proposal, propose
from projectx.spine.gateway.contracts import ChatMessage, ModelRequest
from projectx.spine.gateway.offline import offline_generator
from projectx.spine.jobs.registry import JobContext, job
from projectx.spine.ledger.service import Actor, append
from projectx.spine.tenancy.models import Business

log = structlog.get_logger("gtm.brand")


# ------------------------------------------------------------------ schemas for the model


class Color(BaseModel):
    name: str
    hex: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    role: str = Field(description="primary | secondary | accent | neutral")


class Voice(BaseModel):
    tone: list[str] = Field(description="3-5 adjectives")
    personality: str
    vocabulary_use: list[str]
    vocabulary_avoid: list[str]
    reading_level: str
    emoji_policy: str = Field(description="none | sparing | expressive")
    sample_lines: list[str]


class Product(BaseModel):
    name: str
    description: str
    price: str


class Offer(BaseModel):
    summary: str
    value_proposition: str
    products: list[Product]


class Objection(BaseModel):
    objection: str
    answer: str


class Positioning(BaseModel):
    statement: str
    differentiators: list[str]
    objections: list[Objection]


class ProofPoint(BaseModel):
    claim: str
    evidence: str


class AudienceDraft(BaseModel):
    name: str
    description: str
    segment: str = Field(description="b2c | b2b")
    pains: list[str]
    desires: list[str]
    objections: list[str]
    buying_triggers: list[str]
    platform_affinity: dict[str, float]


class BrandBookDraft(BaseModel):
    summary: str
    voice: Voice
    offer: Offer
    audience_summary: str
    positioning: Positioning
    colors: list[Color]
    do_list: list[str]
    dont_list: list[str]
    banned_words: list[str]
    proof_points: list[ProofPoint]
    hashtags: list[str]
    primary_audience: AudienceDraft


SYSTEM_PROMPT = """You are a senior brand strategist. From what a business says about itself, write a precise,
usable brand book: a voice someone else can write in, an honest offer, a sharp positioning, the audience to
target and where they spend time. Use only facts present in the material for claims and proof points; if
there is no evidence for a proof point, leave it out. Keep every item short and concrete."""

_TONES = {
    "media": ["clear", "curious", "trustworthy", "quick"],
    "service": ["expert", "warm", "direct", "practical"],
    "software": ["helpful", "confident", "precise", "friendly"],
    "ecommerce": ["inviting", "playful", "confident", "clear"],
    "creator": ["personal", "honest", "energetic", "funny"],
}
_AFFINITY = {
    "media": {
        "instagram": 0.8,
        "x": 0.7,
        "tiktok": 0.7,
        "youtube": 0.6,
        "threads": 0.5,
        "telegram": 0.5,
        "discord": 0.4,
        "linkedin": 0.3,
    },
    "service": {"linkedin": 0.9, "instagram": 0.5, "youtube": 0.4, "x": 0.4, "facebook": 0.3},
    "software": {"linkedin": 0.8, "x": 0.8, "youtube": 0.6, "reddit": 0.5, "discord": 0.5},
    "ecommerce": {"instagram": 0.9, "tiktok": 0.9, "pinterest": 0.7, "facebook": 0.5, "youtube": 0.4},
    "creator": {"tiktok": 0.9, "instagram": 0.9, "youtube": 0.8, "x": 0.5},
}


@offline_generator("gtm.brand.draft")
def _offline_brand_book(req: ModelRequest) -> dict[str, Any]:
    c = req.context
    name = c.get("business_name", "The business")
    model = c.get("business_model", "service")
    site = c.get("site") or {}
    description = c.get("description") or site.get("description") or f"{name} helps its customers get results."
    colors = [
        {
            "name": "Brand",
            "hex": (site.get("theme_color") or (site.get("colors") or ["#0071e3"])[0])[:7],
            "role": "primary",
        }
    ]
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", colors[0]["hex"]):
        colors[0]["hex"] = "#0071e3"
    tone = _TONES.get(model, _TONES["service"])
    audience_name = {
        "media": "Busy, curious readers",
        "service": "Founders and operators who need it done right",
        "software": "Teams who want less busywork",
        "ecommerce": "Shoppers who care about quality",
        "creator": "Fans who want the real story",
    }.get(model, "People who need what we offer")
    return {
        "summary": description[:400],
        "voice": {
            "tone": tone,
            "personality": f"{name} sounds {tone[0]} and {tone[1]}, like a smart friend.",
            "vocabulary_use": ["plain words", "specific numbers", "active verbs"],
            "vocabulary_avoid": ["synergy", "revolutionary", "game-changing", "unlock"],
            "reading_level": "Grade 7: short sentences, everyday words",
            "emoji_policy": "sparing",
            "sample_lines": [
                "Here's what changed today, and why it matters to you.",
                f"{name}: the short version, minus the noise.",
            ],
        },
        "offer": {
            "summary": description[:200],
            "value_proposition": f"{name} saves you time and gets you results.",
            "products": [],
        },
        "audience_summary": audience_name,
        "positioning": {
            "statement": f"For {audience_name.lower()}, {name} is the clearest way to get what matters.",
            "differentiators": ["Fast", "Honest", "Specific"],
            "objections": [{"objection": "I don't have time", "answer": "It takes five minutes."}],
        },
        "colors": colors,
        "do_list": ["Lead with the most useful fact", "Use numbers and examples", "End with one clear next step"],
        "dont_list": ["Overpromise", "Use jargon", "Post without a point"],
        "banned_words": ["guaranteed", "revolutionary", "game-changer"],
        "proof_points": [],
        "hashtags": [f"#{re.sub(r'[^A-Za-z0-9]', '', name)}"][:1],
        "primary_audience": {
            "name": audience_name,
            "description": f"The people {name} serves best.",
            "segment": "b2b" if model in ("service", "software") else "b2c",
            "pains": ["Too little time", "Too much noise"],
            "desires": ["Clear answers", "Results"],
            "objections": ["Is it worth it?"],
            "buying_triggers": ["A recommendation from a peer"],
            "platform_affinity": _AFFINITY.get(model, _AFFINITY["service"]),
        },
    }


# ------------------------------------------------------------------ queries


async def current_book(session: AsyncSession, business_id: uuid.UUID) -> BrandBook | None:
    book: BrandBook | None = await session.scalar(
        select(BrandBook)
        .where(BrandBook.business_id == business_id, BrandBook.status == "approved")
        .order_by(BrandBook.version.desc())
        .limit(1)
    )
    return book


async def latest_draft(session: AsyncSession, business_id: uuid.UUID) -> BrandBook | None:
    book: BrandBook | None = await session.scalar(
        select(BrandBook)
        .where(BrandBook.business_id == business_id, BrandBook.status == "draft")
        .order_by(BrandBook.version.desc())
        .limit(1)
    )
    return book


async def _next_version(session: AsyncSession, business_id: uuid.UUID) -> int:
    top = await session.scalar(select(func.max(BrandBook.version)).where(BrandBook.business_id == business_id))
    return int(top or 0) + 1


# ------------------------------------------------------------------ drafting


async def generate_draft(
    services: Services, business: Business, *, site: SiteSummary | None, notes: str | None
) -> BrandBookDraft:
    material = [f"Business: {business.name}", f"Model: {business.business_model}"]
    if business.description:
        material.append(f"Owner's description: {business.description}")
    if notes:
        material.append(f"Owner's notes: {notes}")
    if site is not None:
        material += [
            f"Website title: {site.title}",
            f"Website description: {site.description}",
            "Headings: " + " | ".join(site.headings[:15]),
            f"Website text:\n{site.text[:5000]}",
            f"Theme colour: {site.theme_color}",
            f"Colours used: {', '.join(site.colors)}",
            f"Social profiles: {', '.join(site.socials)}",
        ]
    request = ModelRequest(
        task="gtm.brand.draft",
        system=SYSTEM_PROMPT,
        messages=[ChatMessage("user", "\n".join(material) + "\n\nWrite the brand book.")],
        output_schema=BrandBookDraft,
        max_tokens=6000,
        effort="medium",
        rung="mid",
        context={
            "business_name": business.name,
            "business_model": business.business_model,
            "description": business.description,
            "site": site.as_dict() if site else None,
        },
    )
    result = await services.gateway.run(business.id, request)
    return result.parsed_as(BrandBookDraft)


async def save_draft(
    session: AsyncSession, business: Business, draft: BrandBookDraft, *, sources: dict[str, Any], created_by: str
) -> BrandBook:
    book = BrandBook(
        business_id=business.id,
        version=await _next_version(session, business.id),
        status="draft",
        summary=draft.summary,
        voice=draft.voice.model_dump(),
        offer=draft.offer.model_dump(),
        audience_summary=draft.audience_summary,
        positioning=draft.positioning.model_dump(),
        visual={"colors": [c.model_dump() for c in draft.colors]},
        do_list=draft.do_list,
        dont_list=draft.dont_list,
        banned_words=[w.lower() for w in draft.banned_words],
        proof_points=[p.model_dump() for p in draft.proof_points],
        hashtags=draft.hashtags,
        sources={**sources, "primary_audience": draft.primary_audience.model_dump()},
        created_by=created_by,
    )
    session.add(book)
    await session.flush()
    await append(
        session,
        business_id=business.id,
        type="gtm.brand_book.drafted",
        actor=Actor.agent("pack:gtm.brand"),
        subject=("brand_book", book.id),
        data={"brand_book_id": str(book.id), "version": book.version},
    )
    return book


@job("gtm.draft_brand_book", max_attempts=3, timeout_s=300, priority=30)
async def draft_brand_book_job(ctx: JobContext) -> dict[str, Any] | None:
    business_id = ctx.require_business()
    async with ctx.services.db.session() as session:
        business = await session.get(Business, business_id)
    if business is None:
        return None
    site = None
    url = ctx.payload.get("website") or business.website
    if url:
        try:
            site = await read_site(url)
        except Exception as exc:  # a broken website must not block the brand book
            log.warning("brand.site_unreadable", url=url, error=str(exc))
    draft = await generate_draft(ctx.services, business, site=site, notes=ctx.payload.get("notes"))
    async with ctx.services.db.tenant(business_id) as session:
        biz = await session.get(Business, business_id)
        assert biz is not None
        book = await save_draft(
            session,
            biz,
            draft,
            sources={"website": url, "site": site.as_dict() if site else None},
            created_by="agent:gtm.brand",
        )
        await propose(
            session,
            business_id,
            Proposal(
                action_type="gtm.adopt_brand_book",
                title=f"Adopt brand book v{book.version}",
                summary=draft.summary[:280],
                payload={"brand_book_id": str(book.id)},
                preview={
                    "voice": draft.voice.model_dump(),
                    "positioning": draft.positioning.model_dump(),
                    "do": draft.do_list,
                    "dont": draft.dont_list,
                },
                why_now="Every post, reply and ad will be written from it.",
                if_no="Edit it, or keep the current one.",
                subject=("brand_book", book.id),
                requested_by="pack:gtm.brand",
                priority=20,
            ),
        )
    return {"brand_book_id": str(book.id)}


async def approve_book(
    session: AsyncSession, business: Business, book: BrandBook, *, user_id: uuid.UUID | None
) -> BrandBook:
    if book.status == "approved":
        raise Conflict("This version is already approved")
    await session.execute(
        update(BrandBook)
        .where(BrandBook.business_id == business.id, BrandBook.status == "approved")
        .values(status="archived")
    )
    book.status, book.approved_by, book.approved_at = "approved", user_id, utcnow()
    audience = (book.sources or {}).get("primary_audience")
    if audience:
        existing = await session.scalar(
            select(Audience).where(Audience.business_id == business.id, Audience.is_primary.is_(True))
        )
        if existing is None:
            session.add(
                Audience(
                    business_id=business.id,
                    is_primary=True,
                    name=audience["name"],
                    description=audience["description"],
                    segment=audience.get("segment", "b2c"),
                    pains=audience.get("pains", []),
                    desires=audience.get("desires", []),
                    objections=audience.get("objections", []),
                    buying_triggers=audience.get("buying_triggers", []),
                    platform_affinity=audience.get("platform_affinity", {}),
                )
            )
    business.onboarding = {**(business.onboarding or {}), "brand_book": "approved"}
    await append(
        session,
        business_id=business.id,
        type="gtm.brand_book.approved",
        actor=Actor.user(user_id) if user_id else Actor.system("gtm"),
        subject=("brand_book", book.id),
        data={"brand_book_id": str(book.id), "version": book.version},
    )
    return book


@executor("gtm.adopt_brand_book")
async def _adopt(ctx: ExecutionContext) -> ExecutionResult:
    async with ctx.services.db.tenant(ctx.business_id) as session:
        business = await session.get(Business, ctx.business_id)
        book = await session.get(BrandBook, uuid.UUID(ctx.payload["brand_book_id"]))
        if business is None or book is None:
            raise NotFound("Brand book not found")
        if book.status != "approved":
            await approve_book(session, business, book, user_id=None)
    return ExecutionResult(data={"brand_book_id": ctx.payload["brand_book_id"]}, summary="Brand book adopted")


def book_view(book: BrandBook) -> dict[str, Any]:
    return {
        "id": str(book.id),
        "version": book.version,
        "status": book.status,
        "summary": book.summary,
        "voice": book.voice,
        "offer": book.offer,
        "audience_summary": book.audience_summary,
        "positioning": book.positioning,
        "visual": book.visual,
        "do_list": book.do_list,
        "dont_list": book.dont_list,
        "banned_words": book.banned_words,
        "proof_points": book.proof_points,
        "hashtags": book.hashtags,
        "created_by": book.created_by,
        "approved_at": book.approved_at.isoformat() if book.approved_at else None,
        "created_at": book.created_at.isoformat(),
        "primary_audience": (book.sources or {}).get("primary_audience"),
    }


def audience_view(a: Audience) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "name": a.name,
        "is_primary": a.is_primary,
        "description": a.description,
        "segment": a.segment,
        "demographics": a.demographics,
        "firmographics": a.firmographics,
        "pains": a.pains,
        "desires": a.desires,
        "objections": a.objections,
        "buying_triggers": a.buying_triggers,
        "platform_affinity": a.platform_affinity,
        "locations": a.locations,
    }
