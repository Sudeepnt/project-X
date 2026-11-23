"""Every check a content item passes before it can be proposed for publishing:
brand voice, claims, duplicates (own history and across all accounts), platform specs and AI labels."""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.time import utcnow
from projectx.modules.gtm.brand.check import CheckResult, brand_check, claims_check, find_claims
from projectx.modules.gtm.config import gtm_settings
from projectx.modules.gtm.content.fingerprint import NEAR_DUPLICATE_BITS, band, distance, simhash, to_signed
from projectx.modules.gtm.goals import restrictions as restrictions_svc
from projectx.modules.gtm.models import BrandBook, ContentFingerprint, ContentItem, ContentVariant
from projectx.modules.gtm.platforms import PLATFORMS
from projectx.spine.tenancy.models import Business

_URL = re.compile(r"https?://\S+")
_HASHTAG = re.compile(r"(?<!\w)#\w+")
VIDEO_FORMATS = {"vertical_video", "long_video"}
MEDIA_FORMATS = VIDEO_FORMATS | {"feed_image", "carousel", "story", "photo_post", "pin", "doc_carousel"}


def spec_check(variant: ContentVariant, *, practice: bool) -> CheckResult:
    spec = PLATFORMS.get(variant.platform)
    if spec is None:
        return CheckResult("specs", False, "block", [f"Unknown platform {variant.platform}"])
    issues: list[str] = []
    severity = "info"
    parts = [variant.text, *[str(p) for p in (variant.thread or [])]]
    for i, part in enumerate(parts):
        if len(part) > spec.text_limit:
            issues.append(f"Part {i + 1} is {len(part)} characters; {spec.label} allows {spec.text_limit}")
            severity = "block"
    tags = _HASHTAG.findall(variant.text)
    if len(tags) > spec.hashtag_limit:
        issues.append(f"{len(tags)} hashtags; {spec.label} allows {spec.hashtag_limit}")
        severity = "block"
    links = _URL.findall(" ".join(parts))
    if links and spec.links_in_text == "no":
        issues.append(f"{spec.label} captions can't carry clickable links; say 'link in bio'")
        severity = "block"
    if spec.extra.get("max_links") and len(links) > spec.extra["max_links"]:
        issues.append(f"More than {spec.extra['max_links']} links fails on {spec.label}")
        severity = "block"
    if variant.format in MEDIA_FORMATS and not variant.media and not practice:
        issues.append("Needs an image or video before it can be posted")
        severity = "review" if severity == "info" else severity
    return CheckResult("specs", severity != "block", severity, issues)


async def duplicate_check(
    session: AsyncSession, business_id: uuid.UUID, text: str, exclude_id: uuid.UUID | None
) -> tuple[CheckResult, int]:
    fp = simhash(text)
    since = utcnow() - timedelta(days=60)
    own = (
        await session.execute(
            select(ContentItem.id, ContentItem.simhash).where(
                ContentItem.business_id == business_id,
                ContentItem.simhash.is_not(None),
                ContentItem.created_at >= since,
                ContentItem.status.notin_(("rejected", "archived")),
            )
        )
    ).all()
    issues, severity = [], "info"
    if any(cid != exclude_id and distance(h, fp) <= NEAR_DUPLICATE_BITS for cid, h in own):
        issues.append("Almost the same as a recent post")
        severity = "review"
    others = (
        (
            await session.execute(
                select(ContentFingerprint.simhash)
                .where(
                    ContentFingerprint.band == band(fp),
                    ContentFingerprint.business_id != business_id,
                    ContentFingerprint.created_at >= since,
                )
                .limit(500)
            )
        )
        .scalars()
        .all()
    )
    if any(distance(h, fp) <= NEAR_DUPLICATE_BITS for h in others):
        issues.append("The same text is already posted by another account on the platform")
        severity = "block"
    return CheckResult("duplicates", severity != "block", severity, issues), to_signed(fp)


async def run_checks(
    session: AsyncSession, business: Business, book: BrandBook | None, item: ContentItem, variants: list[ContentVariant]
) -> dict[str, Any]:
    rules = await restrictions_svc.active(session, business.id)
    core_text = f"{item.title}\n{item.body}\n{item.cta or ''}"
    results: dict[str, Any] = {}
    brand = brand_check(core_text, book, extra_banned=restrictions_svc.banned_phrases(rules))
    evidence = [f"{p.get('claim', '')} {p.get('evidence', '')}" for p in (book.proof_points if book else [])]
    source = item.source_ref or {}
    evidence.append(f"{source.get('title', '')} {source.get('summary', '')}")
    claims = claims_check(
        list(item.claims or []) or find_claims(core_text),
        evidence=evidence,
        strict=restrictions_svc.claims_strict(rules),
    )
    dup, fp = await duplicate_check(session, business.id, core_text, item.id)
    item.simhash = fp
    results.update(brand=brand.as_dict(), claims=claims.as_dict(), duplicates=dup.as_dict())
    label_policy = gtm_settings(business)["ai_label"]
    for variant in variants:
        vbrand = brand_check(
            variant.text + " " + " ".join(map(str, variant.thread or [])),
            book,
            extra_banned=restrictions_svc.banned_phrases(rules),
        )
        specs = spec_check(variant, practice=business.practice_mode)
        spec = PLATFORMS.get(variant.platform)
        ai_label = (
            "api_flag" if spec and spec.ai_label == "api_flag" else ("text" if label_policy == "text" else "none")
        )
        if ai_label == "text" and "#ai" not in variant.text.lower() and item.ai_generated:
            variant.text = (
                f"{variant.text}\n\n(Made with AI)"
                if spec and len(variant.text) + 16 <= spec.text_limit
                else variant.text
            )
        variant.spec = {"brand": vbrand.as_dict(), "specs": specs.as_dict(), "ai_label": ai_label}
        severities = {
            vbrand.severity if not vbrand.passed else "info",
            specs.severity if not specs.passed else ("review" if specs.issues else "info"),
        }
        variant.status = "blocked" if "block" in severities else ("needs_review" if "review" in severities else "ready")
    all_results = [brand, claims, dup]
    if any(not r.passed and r.severity == "block" for r in all_results) or all(v.status == "blocked" for v in variants):
        outcome = "blocked"
    elif any((not r.passed) or r.severity == "review" for r in all_results) or any(
        v.status == "needs_review" for v in variants
    ):
        outcome = "review"
    else:
        outcome = "passed"
    results["outcome"] = outcome
    return results
