"""Deterministic checks every piece of content passes before it can be published.

The brand check enforces the approved brand book (banned words, phrases to avoid, tone limits,
emoji policy). The claims check enforces "no claims without proof": a factual claim must be
backed by a proof point or by the source material it came from. Both are code, so they are
cheap, instant and impossible to talk past; a model review can add nuance on top.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from projectx.modules.gtm.models import BrandBook

_EMOJI = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff\U00002190-\U000021ff\U00002b00-\U00002bff]"
)
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]{3,}")
_NUMBER_CLAIM = re.compile(r"\b(\d+(\.\d+)?\s?(%|x|times|percent|k|m|million|billion|crore|lakh))(?!\w)", re.IGNORECASE)
_SUPERLATIVE = re.compile(r"\b(best|#1|number one|fastest|cheapest|only|guaranteed|proven|clinically)\b", re.IGNORECASE)


@dataclass
class CheckResult:
    name: str
    passed: bool
    severity: str = "info"  # block | review | info
    issues: list[str] = field(default_factory=list)
    score: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def count_emoji(text: str) -> int:
    return len(_EMOJI.findall(text))


def brand_check(text: str, book: BrandBook | None, *, extra_banned: list[str] | None = None) -> CheckResult:
    issues: list[str] = []
    severity = "info"
    lowered = text.lower()
    banned = {w.strip().lower() for w in (book.banned_words if book else []) + (extra_banned or []) if w.strip()}
    avoid = {w.strip().lower() for w in ((book.voice or {}).get("vocabulary_avoid", []) if book else []) if w.strip()}
    hits = sorted(w for w in banned if re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", lowered))
    if hits:
        issues.append("Uses words the brand never uses: " + ", ".join(hits))
        severity = "block"
    soft = sorted(w for w in avoid if re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", lowered))
    if soft:
        issues.append("Uses words the brand avoids: " + ", ".join(soft))
        severity = "review" if severity == "info" else severity
    if text.count("!") > 2:
        issues.append("Too many exclamation marks")
    words = _WORD.findall(text)
    if words and sum(1 for w in words if w.isupper()) / len(words) > 0.25:
        issues.append("Too much text in capitals")
    policy = (book.voice or {}).get("emoji_policy", "sparing") if book else "sparing"
    emojis = count_emoji(text)
    if policy == "none" and emojis:
        issues.append("The brand doesn't use emoji")
    elif policy == "sparing" and emojis > 3:
        issues.append(f"{emojis} emoji: the brand uses them sparingly")
    for phrase in book.dont_list if book else []:
        if len(phrase) > 6 and phrase.lower() in lowered:
            issues.append(f"Breaks a brand rule: {phrase}")
            severity = "review" if severity == "info" else severity
    score = max(0.0, 1.0 - 0.2 * len(issues))
    passed = severity != "block" and score >= 0.6
    if not passed and severity == "info":
        severity = "review"
    return CheckResult("brand", passed, severity, issues, round(score, 2))


def find_claims(text: str) -> list[str]:
    """Sentences that state a number or a superlative: the ones that need evidence."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return [s.strip() for s in sentences if s.strip() and (_NUMBER_CLAIM.search(s) or _SUPERLATIVE.search(s))][:10]


def claims_check(claims: list[str], *, evidence: list[str], strict: bool) -> CheckResult:
    """A claim passes when a significant part of it appears in the evidence (proof points or source)."""
    corpus = " ".join(evidence).lower()
    unproven = []
    for claim in claims:
        numbers = _NUMBER_CLAIM.findall(claim)
        supported = bool(numbers) and all(n[0].lower() in corpus for n in numbers)
        if not supported and not numbers:
            key_words = [w.lower() for w in _WORD.findall(claim)][:6]
            supported = bool(key_words) and sum(1 for w in key_words if w in corpus) >= max(2, len(key_words) // 2)
        if not supported:
            unproven.append(claim)
    if not unproven:
        return CheckResult("claims", True, "info", [], 1.0)
    issues = [f"No evidence for: {c[:140]}" for c in unproven]
    return CheckResult("claims", not strict, "review", issues, round(1 - len(unproven) / max(len(claims), 1), 2))
