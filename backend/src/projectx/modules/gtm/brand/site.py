"""Reading a business's website so the brand book can be drafted from it."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from projectx.spine.safe_fetch import fetch_public

_SOCIAL = {
    "instagram.com": "instagram",
    "tiktok.com": "tiktok",
    "x.com": "x",
    "twitter.com": "x",
    "linkedin.com": "linkedin",
    "youtube.com": "youtube",
    "facebook.com": "facebook",
    "threads.net": "threads",
    "pinterest.com": "pinterest",
    "discord.gg": "discord",
    "t.me": "telegram",
}
_HEX = re.compile(r"#[0-9a-fA-F]{6}\b")


@dataclass
class SiteSummary:
    url: str
    title: str = ""
    description: str = ""
    headings: list[str] = field(default_factory=list)
    text: str = ""
    theme_color: str | None = None
    colors: list[str] = field(default_factory=list)
    socials: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "description": self.description,
            "headings": self.headings,
            "text": self.text,
            "theme_color": self.theme_color,
            "colors": self.colors,
            "socials": self.socials,
        }


def summarize_html(url: str, html: str, *, max_text: int = 6000) -> SiteSummary:
    tree = HTMLParser(html)
    for node in tree.css("script, style, noscript, svg, iframe"):
        node.decompose()

    def meta(selector: str) -> str:
        node = tree.css_first(selector)
        return (node.attributes.get("content") or "").strip() if node else ""

    title_node = tree.css_first("title")
    summary = SiteSummary(
        url=url,
        title=(title_node.text(strip=True) if title_node else "") or meta('meta[property="og:title"]'),
        description=meta('meta[name="description"]') or meta('meta[property="og:description"]'),
        theme_color=meta('meta[name="theme-color"]') or None,
    )
    summary.headings = [h.text(strip=True) for h in tree.css("h1, h2, h3") if h.text(strip=True)][:25]
    paragraphs = [p.text(strip=True) for p in tree.css("p, li") if len(p.text(strip=True)) > 30]
    summary.text = "\n".join(paragraphs)[:max_text]
    for a in tree.css("a[href]"):
        href = a.attributes.get("href") or ""
        for domain, platform in _SOCIAL.items():
            if domain in href and platform not in summary.socials:
                summary.socials[platform] = href
    style_text = " ".join(s.text() for s in HTMLParser(html).css("style"))[:50_000]
    seen: dict[str, int] = {}
    for color in _HEX.findall(style_text):
        lowered = color.lower()
        if lowered not in ("#ffffff", "#000000"):
            seen[lowered] = seen.get(lowered, 0) + 1
    summary.colors = [c for c, _ in sorted(seen.items(), key=lambda kv: -kv[1])][:5]
    return summary


async def read_site(url: str) -> SiteSummary:
    result = await fetch_public(url)
    if "html" not in result.content_type.lower():
        return SiteSummary(url=result.url)
    return summarize_html(result.url, result.text)
