"""Repurposing: one long piece (article, transcript, newsletter) becomes several distinct posts.

The model splits the source into standalone ideas, each with its own angle and hook. Every idea then goes
through the normal pipeline (write in the brand voice, adapt per platform, checks, approval), and the source
text is the only material a post may draw facts from.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.errors import ValidationFailed
from projectx.kernel.time import utcnow
from projectx.modules.gtm.brand.service import current_book
from projectx.modules.gtm.content.generate import brand_system_prompt
from projectx.modules.gtm.content.hooks import DEFAULT_HOOKS
from projectx.modules.gtm.models import Campaign, ContentItem
from projectx.modules.gtm.platforms import PLATFORMS, format_for
from projectx.spine.gateway.contracts import ChatMessage, ComputeCapReached, ModelRequest, ModelsRateLimited
from projectx.spine.gateway.offline import offline_generator
from projectx.spine.jobs.queue import enqueue
from projectx.spine.jobs.registry import JobContext, RetryLater, job
from projectx.spine.tenancy.models import Business

MIN_CHARS, MAX_CHARS = 400, 60_000
PILLARS = ("educate", "entertain", "inspire", "behind_the_scenes", "proof")


class Idea(BaseModel):
    title: str = Field(max_length=200)
    angle: str = Field(max_length=300, description="What makes this post different from the others")
    summary: str = Field(max_length=900, description="The facts from the source this post uses, nothing else")
    hook_type: str = Field(description="One of: " + ", ".join(DEFAULT_HOOKS))
    pillar: Literal["educate", "entertain", "inspire", "behind_the_scenes", "proof"] = "educate"


class Split(BaseModel):
    ideas: list[Idea] = Field(min_length=1, max_length=10)


def _chunks(text: str) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if len(p.strip()) > 60]
    if len(paragraphs) >= 2:
        return paragraphs
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    size = max(2, len(sentences) // 5)
    return [" ".join(sentences[i : i + size]) for i in range(0, len(sentences), size)]


@offline_generator("gtm.content.repurpose")
def _offline_split(req: ModelRequest) -> dict[str, Any]:
    """Deterministic stand-in: the longest distinct passages become ideas."""
    text, count = str(req.context.get("text", "")), int(req.context.get("count", 5))
    chunks = sorted(_chunks(text), key=len, reverse=True)[:count]
    ideas = []
    for i, chunk in enumerate(chunks):
        first = re.split(r"(?<=[.!?])\s+", chunk)[0]
        title = first[:80].rstrip(".,;:") or f"Part {i + 1}"
        ideas.append(
            {
                "title": title,
                "angle": f"Takeaway {i + 1} of {len(chunks)}",
                "summary": chunk[:900],
                "hook_type": DEFAULT_HOOKS[i % len(DEFAULT_HOOKS)],
                "pillar": PILLARS[i % len(PILLARS)],
            }
        )
    return {"ideas": ideas}


def _distinct(split: Split) -> str | None:
    titles = [re.sub(r"\W+", " ", i.title.lower()).strip() for i in split.ideas]
    return None if len(set(titles)) == len(titles) else "Two ideas share a title; make every idea distinct"


async def queue_repurpose(
    session: AsyncSession,
    business: Business,
    *,
    title: str,
    text: str,
    source_url: str | None,
    count: int,
    platforms: list[str] | None,
    campaign_id: uuid.UUID | None,
) -> uuid.UUID | None:
    text = text.strip()
    if not MIN_CHARS <= len(text) <= MAX_CHARS:
        raise ValidationFailed(f"Paste between {MIN_CHARS:,} and {MAX_CHARS:,} characters")
    if not 2 <= count <= 10:
        raise ValidationFailed("Make between 2 and 10 posts")
    unknown = [p for p in platforms or [] if p not in PLATFORMS]
    if unknown:
        raise ValidationFailed(f"Unknown platform: {', '.join(unknown)}")
    digest = hashlib.sha256(text.encode()).hexdigest()[:24]
    return await enqueue(
        session,
        "gtm.repurpose",
        {
            "title": title[:200],
            "text": text,
            "source_url": source_url,
            "count": count,
            "platforms": platforms,
            "campaign_id": str(campaign_id) if campaign_id else None,
            "digest": digest,
        },
        business_id=business.id,
        idempotency_key=f"repurpose:{digest}:{count}",
    )


@job("gtm.repurpose", max_attempts=5, timeout_s=300, priority=40)
async def repurpose(ctx: JobContext) -> dict[str, Any]:
    business_id = ctx.require_business()
    p = ctx.payload
    async with ctx.services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        assert business is not None
        book = await current_book(session, business_id)
    try:
        result = await ctx.services.gateway.run(
            business_id,
            ModelRequest(
                task="gtm.content.repurpose",
                system=brand_system_prompt(business, book)
                + "\nSplit the source into distinct, standalone post ideas. Each idea must stand on its own, take a "
                "different angle, and use only facts that appear in the source. Never invent numbers or claims.",
                messages=[
                    ChatMessage(
                        "user", f"Make {p['count']} ideas from this source titled {p['title']!r}:\n\n{p['text']}"
                    )
                ],
                output_schema=Split,
                max_tokens=3000,
                effort="medium",
                rung="mid",
                context={"text": p["text"], "count": p["count"], "title": p["title"]},
                verifier=lambda r: _distinct(r.parsed_as(Split)),
            ),
        )
    except ComputeCapReached as exc:
        raise RetryLater(3600, "daily compute cap reached") from exc
    except ModelsRateLimited as exc:
        raise RetryLater(exc.retry_after, "model provider rate limit") from exc
    split = result.parsed_as(Split)
    created: list[str] = []
    async with ctx.services.db.tenant(business_id) as session:
        campaign = await session.get(Campaign, uuid.UUID(p["campaign_id"])) if p.get("campaign_id") else None
        if campaign is None:
            campaign = await session.scalar(
                select(Campaign)
                .where(Campaign.business_id == business_id, Campaign.status == "active")
                .order_by(Campaign.role, Campaign.created_at)
                .limit(1)
            )
        channels = (
            p.get("platforms") or (campaign.channels if campaign else None) or ["instagram", "x", "linkedin", "threads"]
        )
        for i, idea in enumerate(split.ideas[: int(p["count"])]):
            kind = "text" if i % 2 else "image"
            formats = {pl: f for pl in channels if (f := format_for(pl, kind)) is not None}
            if not formats:
                continue
            item = ContentItem(
                business_id=business_id,
                campaign_id=campaign.id if campaign else None,
                pillar=idea.pillar,
                intent="value",
                hook_type=idea.hook_type if idea.hook_type in DEFAULT_HOOKS else "curiosity_gap",
                title=idea.title,
                core_idea=idea.summary[:600],
                source_type="repurposed",
                status="idea",
                planned_for=utcnow() + timedelta(days=i + 1),  # spread out, one a day
                source_ref={
                    "repurposed_from": p["digest"],
                    "source_title": p["title"],
                    "url": p.get("source_url"),
                    "title": idea.title,
                    "summary": f"{idea.angle}. {idea.summary}",
                    "formats": formats,
                    "platforms": list(formats),
                    "mix_kind": kind,
                    "theme_name": p["title"][:60] or "Repurposed",
                },
            )
            session.add(item)
            await session.flush()
            await enqueue(
                session,
                "gtm.generate_content",
                {"content_id": str(item.id)},
                business_id=business_id,
                idempotency_key=f"gen:{item.id}",
            )
            created.append(str(item.id))
    return {"created": created}
