"""Writing content from the brand book: one core piece, then a native version per platform.

The system prompt holds the brand book and the rules, and stays byte-identical between calls so
providers can cache it. Numbers in a post may only come from the source material or the brand
book's proof points; the claims check enforces that afterwards.
"""

from __future__ import annotations

import re
import textwrap
from typing import Any

from pydantic import BaseModel, Field

from projectx.modules.gtm.content.hooks import HOOKS
from projectx.modules.gtm.models import BrandBook
from projectx.modules.gtm.platforms import FORMAT_LABELS, PLATFORMS
from projectx.services import Services
from projectx.spine.gateway.contracts import ChatMessage, ModelRequest
from projectx.spine.gateway.offline import offline_generator, stable_pick
from projectx.spine.tenancy.models import Business


class CoreDraft(BaseModel):
    title: str = Field(max_length=200)
    hook_text: str = Field(max_length=300)
    core_idea: str = Field(max_length=600)
    body: str = Field(max_length=4000)
    cta: str = Field(max_length=300)
    claims: list[str] = Field(max_length=10)
    hashtags: list[str] = Field(max_length=10)
    visual_brief: str = Field(max_length=600)


class VariantDraft(BaseModel):
    platform: str
    format: str
    text: str = Field(max_length=6000)
    title: str = Field(max_length=200)
    thread: list[str] = Field(max_length=12)
    hashtags: list[str] = Field(max_length=30)
    alt_text: str = Field(max_length=500)


class VariantsDraft(BaseModel):
    variants: list[VariantDraft]


def brand_system_prompt(business: Business, book: BrandBook | None) -> str:
    if book is None:
        voice = "Clear, warm, specific. Short sentences. No hype."
        rules = ""
    else:
        v = book.voice or {}
        voice = (
            f"Tone: {', '.join(v.get('tone', []))}. {v.get('personality', '')} Reading level: "
            f"{v.get('reading_level', 'plain')}. Emoji: {v.get('emoji_policy', 'sparing')}."
        )
        rules = textwrap.dedent(f"""
            Positioning: {(book.positioning or {}).get("statement", "")}
            Audience: {book.audience_summary}
            Offer: {(book.offer or {}).get("summary", "")}
            Always: {"; ".join(book.do_list)}
            Never: {"; ".join(book.dont_list)}
            Words never used: {", ".join(book.banned_words)}
            Proof points you may cite: {"; ".join(p.get("claim", "") for p in book.proof_points) or "none"}
        """).strip()
    return textwrap.dedent(f"""
        You write social and email content for {business.name}, in its own voice, native to each platform.
        Voice: {voice}
        {rules}
        Rules:
        - Every number or factual claim must come from the source material or the proof points above. Never invent one.
        - Value posts teach, entertain or inspire with no sales pitch. Ask posts end with one soft, specific next step.
        - Open with the hook. One idea per post. Write for a phone screen.
        - No hashtags inside sentences; put a few relevant ones at the end only where the platform rewards them.
    """).strip()


async def write_core(
    services: Services,
    business: Business,
    book: BrandBook | None,
    *,
    slot: dict[str, Any],
    source: dict[str, Any] | None,
    notes: str,
) -> CoreDraft:
    hook = HOOKS.get(slot.get("hook_type") or "", HOOKS["curiosity_gap"])
    lines = [
        f"Theme: {slot.get('theme_name')} ({slot.get('pillar')})",
        f"Intent: {slot.get('intent')} post",
        f"Hook style: {hook.label} — {hook.pattern}",
        f"Content type: {slot.get('mix_kind')}",
    ]
    if slot.get("moment"):
        lines.append(f"Moment to reference if natural: {slot['moment']['name']} on {slot['moment']['date']}")
    if slot.get("campaign_brief"):
        lines.append(f"Campaign: {slot['campaign_brief']}")
    if source:
        lines.append(
            f"Source material (the only facts you may use):\nTitle: {source.get('title')}\n"
            f"{source.get('summary') or ''}\nLink: {source.get('url') or ''}"
        )
    if notes:
        lines.append(notes)
    request = ModelRequest(
        task="gtm.content.write",
        system=brand_system_prompt(business, book),
        messages=[ChatMessage("user", "\n".join(lines) + "\n\nWrite the core post.")],
        output_schema=CoreDraft,
        max_tokens=3000,
        effort="low",
        rung="small",
        context={
            "business_name": business.name,
            "slot": slot,
            "source": source,
            "hashtags": book.hashtags if book else [],
        },
    )
    result = await services.gateway.run(business.id, request)
    return result.parsed_as(CoreDraft)


def platform_brief(platform: str, fmt: str) -> str:
    spec = PLATFORMS.get(platform)
    if spec is None:
        return f"{platform}: {fmt}"
    link_rule = {
        "no": "links are not clickable in captions, so say 'link in bio' instead of pasting a URL",
        "costly": "a link makes the post cost much more, so only include one when it matters",
        "penalized": "links reduce reach, so put the link in the first comment",
        "yes": "links are fine",
    }[spec.links_in_text]
    return (
        f"{spec.label} — {FORMAT_LABELS.get(fmt, fmt)}: at most {spec.text_limit} characters per post, "
        f"up to {spec.hashtag_limit} hashtags; {link_rule}."
    )


async def adapt_variants(
    services: Services,
    business: Business,
    book: BrandBook | None,
    core: CoreDraft,
    *,
    formats: dict[str, str],
    link: str | None,
) -> VariantsDraft:
    briefs = "\n".join(f"- {platform_brief(p, f)}" for p, f in formats.items())
    message = (
        f"Core post:\nTitle: {core.title}\nHook: {core.hook_text}\n{core.body}\nNext step: {core.cta}\n"
        f"Link (if relevant): {link or 'none'}\n\nWrite one native version for each of these:\n{briefs}\n"
        "For threads, put each part in `thread`. For video formats, `text` is the caption and `title` the "
        "on-screen title."
    )
    request = ModelRequest(
        task="gtm.content.adapt",
        system=brand_system_prompt(business, book),
        messages=[ChatMessage("user", message)],
        output_schema=VariantsDraft,
        max_tokens=6000,
        effort="low",
        rung="small",
        context={"core": core.model_dump(), "formats": formats, "link": link},
    )
    result = await services.gateway.run(business.id, request)
    return result.parsed_as(VariantsDraft)


# ------------------------------------------------------------------ offline drafts

_VALUE_CTAS = ["Save this for later.", "Share it with someone who needs it.", "Tell us what you'd add."]
_ASK_CTAS = [
    "Want the full version? It's free, link in bio.",
    "Reply 'yes' and we'll send it over.",
    "Book a free call this week.",
]


def _topic(slot: dict[str, Any], source: dict[str, Any] | None) -> str:
    if source and source.get("title"):
        return str(source["title"]).rstrip(".")
    return str(slot.get("theme_name") or "this")


@offline_generator("gtm.content.write")
def _offline_core(req: ModelRequest) -> dict[str, Any]:
    slot, source = req.context.get("slot", {}), req.context.get("source")
    topic = _topic(slot, source)
    hook = HOOKS.get(slot.get("hook_type") or "", HOOKS["curiosity_gap"])
    hook_text = hook.example.format(topic=topic.lower() if not source else topic)
    summary = (source or {}).get("summary") or f"{topic}: what changed, why it matters, and what to do next."
    intent = slot.get("intent", "value")
    cta = stable_pick(_ASK_CTAS if intent == "ask" else _VALUE_CTAS, topic, slot.get("hook_type"))
    body = f"{hook_text}\n\n{summary}\n\nThe short version: focus on what you can act on today."
    tags = list(req.context.get("hashtags") or [])[:3]
    return {
        "title": topic[:200],
        "hook_text": hook_text[:300],
        "core_idea": summary[:600],
        "body": body[:4000],
        "cta": cta,
        "claims": [],
        "hashtags": tags,
        "visual_brief": f"Bold title card: '{topic[:60]}'",
    }


def _trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    return cut.rstrip(",;:") + "…"


@offline_generator("gtm.content.adapt")
def _offline_variants(req: ModelRequest) -> dict[str, Any]:
    core = req.context.get("core", {})
    formats: dict[str, str] = req.context.get("formats", {})
    link = req.context.get("link")
    variants = []
    for platform, fmt in formats.items():
        spec = PLATFORMS.get(platform)
        limit = spec.text_limit if spec else 500
        tags = core.get("hashtags", [])[: spec.hashtag_limit if spec else 3]
        body = f"{core.get('hook_text', '')}\n\n{core.get('core_idea', '')}\n\n{core.get('cta', '')}".strip()
        thread: list[str] = []
        if fmt == "thread":
            sentences = [s for s in re.split(r"(?<=[.!?])\s+", core.get("body", "")) if s.strip()]
            thread = [_trim(s, limit) for s in sentences[:5]]
            body = _trim(core.get("hook_text", ""), limit)
        if link and spec and spec.links_in_text == "yes":
            body = f"{body}\n{link}"
        elif link and spec and spec.links_in_text == "no":
            body = body.replace(link, "") + "\nLink in bio."
        if tags and spec and spec.kind == "social":
            body = f"{body}\n\n{' '.join(tags)}"
        variants.append(
            {
                "platform": platform,
                "format": fmt,
                "text": _trim(body, limit),
                "title": _trim(core.get("title", ""), 100),
                "thread": thread,
                "hashtags": tags,
                "alt_text": core.get("visual_brief", "")[:500],
            }
        )
    return {"variants": variants}
