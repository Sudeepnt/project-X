"""Platform knowledge in one place: formats, limits, link rules, API access and approvals.

"Create once, adapt everywhere": one core idea becomes a native version per platform, and the
rules table below sends each format only to the platforms where it performs. Limits come from
each platform's published API rules (late 2026); check them when a platform changes its terms.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Platform:
    key: str
    label: str
    kind: str  # social | messaging | owned | community
    formats: tuple[str, ...]
    text_limit: int
    hashtag_limit: int
    links_in_text: str  # yes | no | costly | penalized
    api_posting: str  # instant | review_required | unaudited_private | manual_only
    daily_post_limit: int | None
    approval_notes: str
    ai_label: str  # api_flag | text | none
    best_for: str
    oauth: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


PLATFORMS: dict[str, Platform] = {
    p.key: p
    for p in (
        Platform(
            "instagram",
            "Instagram",
            "social",
            ("vertical_video", "feed_image", "carousel", "story"),
            2200,
            30,
            "no",
            "review_required",
            100,
            "Publishes images, videos, reels and carousels (a carousel counts as one post); 100 API posts per 24h; "
            "stories need a business account; Meta app review before posting for customers.",
            "none",
            "Reels, carousels, stories",
        ),
        Platform(
            "facebook",
            "Facebook Page",
            "social",
            ("text_post", "feed_image", "vertical_video", "link_post"),
            63206,
            10,
            "yes",
            "review_required",
            None,
            "Same Meta app review as Instagram.",
            "none",
            "Video, link posts, community",
        ),
        Platform(
            "threads",
            "Threads",
            "social",
            ("text_post", "feed_image", "carousel", "thread", "discussion_question"),
            500,
            1,
            "yes",
            "review_required",
            250,
            "Image, video, text or carousel posts; 250 posts per 24h; posts with more than 5 links fail.",
            "none",
            "Short takes, conversation",
            extra={"max_links": 5},
        ),
        Platform(
            "tiktok",
            "TikTok",
            "social",
            ("vertical_video", "photo_post"),
            2200,
            10,
            "no",
            "unaudited_private",
            15,
            "Unaudited apps post privately only, to at most 5 users per 24h; the audit takes days to two weeks; each "
            "creator is capped at about 15 posts a day across every app.",
            "api_flag",
            "Short vertical video, photo posts",
        ),
        Platform(
            "youtube",
            "YouTube",
            "social",
            ("long_video", "vertical_video"),
            5000,
            15,
            "yes",
            "review_required",
            None,
            "Uploads from unverified API projects may stay private until an audit passes; test one upload first.",
            "api_flag",
            "Long videos and Shorts",
        ),
        Platform(
            "linkedin",
            "LinkedIn",
            "social",
            ("text_post", "doc_carousel", "feed_image", "vertical_video"),
            3000,
            5,
            "penalized",
            "review_required",
            None,
            "Posting as yourself is self-serve; company pages need w_organization_social (Community Management API, "
            "Development then Standard tier), a verified company website and domain, and a matching LinkedIn Page.",
            "none",
            "Text, document carousels, founder posts",
        ),
        Platform(
            "x",
            "X",
            "social",
            ("text_post", "thread", "feed_image", "vertical_video"),
            280,
            2,
            "costly",
            "instant",
            None,
            "Pay per post: about $0.015 per post and about $0.20 for a post containing a link.",
            "none",
            "News, short text, threads",
            extra={"cost_usd": 0.015, "cost_link_usd": 0.20},
        ),
        Platform(
            "pinterest",
            "Pinterest",
            "social",
            ("pin",),
            500,
            5,
            "yes",
            "review_required",
            None,
            "Trial access mostly allows reading and Trial pins are visible only to you; Standard access to post.",
            "none",
            "Infographics, long-lasting pins",
        ),
        Platform(
            "reddit",
            "Reddit",
            "community",
            ("text_post", "discussion_question"),
            40000,
            0,
            "yes",
            "manual_only",
            None,
            "Commercial use needs app review and a contract; treat as manual only.",
            "text",
            "Discussion, Q&A sessions",
            oauth=False,
        ),
        Platform(
            "discord",
            "Discord",
            "community",
            ("discussion_question", "channel_post"),
            2000,
            0,
            "yes",
            "instant",
            None,
            "Bot API works instantly.",
            "text",
            "Community, broadcasts",
            oauth=False,
        ),
        Platform(
            "telegram",
            "Telegram",
            "messaging",
            ("channel_post", "discussion_question"),
            4096,
            5,
            "yes",
            "instant",
            None,
            "Bot API works instantly; add the bot as a channel admin.",
            "text",
            "Community, broadcasts",
            oauth=False,
        ),
        Platform(
            "whatsapp",
            "WhatsApp",
            "messaging",
            ("channel_post",),
            1024,
            0,
            "yes",
            "review_required",
            None,
            "Through the WhatsApp Business platform in 1-7 days; people must opt in; marketing needs approved "
            "templates outside the 24-hour window.",
            "none",
            "Broadcasts and one-to-one nurture",
            oauth=False,
        ),
        Platform(
            "newsletter",
            "Newsletter",
            "owned",
            ("newsletter_blurb",),
            100000,
            0,
            "yes",
            "instant",
            None,
            "Your most important channel because you own it.",
            "text",
            "Nurture, daily digest",
            oauth=False,
        ),
        Platform(
            "blog",
            "Blog",
            "owned",
            ("article",),
            100000,
            0,
            "yes",
            "instant",
            None,
            "Yours, instant.",
            "text",
            "SEO, landing pages",
            oauth=False,
        ),
    )
}

FORMAT_LABELS = {
    "vertical_video": "9:16 vertical video",
    "feed_image": "4:5 feed image",
    "carousel": "Carousel",
    "story": "Story",
    "text_post": "Text post",
    "link_post": "Link post",
    "thread": "Thread",
    "doc_carousel": "Document carousel",
    "photo_post": "Photo post",
    "long_video": "Long video",
    "pin": "Pin",
    "discussion_question": "Discussion question",
    "channel_post": "Channel post",
    "newsletter_blurb": "Newsletter blurb",
    "article": "Article",
}

# The rules table: which native format each platform gets for each kind of core idea.
FORMAT_FOR_MIX = {
    "short_video": {
        "instagram": "vertical_video",
        "tiktok": "vertical_video",
        "youtube": "vertical_video",
        "facebook": "vertical_video",
        "linkedin": "vertical_video",
        "x": "text_post",
        "threads": "text_post",
        "telegram": "channel_post",
        "newsletter": "newsletter_blurb",
        "discord": "discussion_question",
    },
    "carousel": {
        "instagram": "carousel",
        "linkedin": "doc_carousel",
        "threads": "carousel",
        "x": "thread",
        "facebook": "feed_image",
        "pinterest": "pin",
        "telegram": "channel_post",
        "newsletter": "newsletter_blurb",
    },
    "image": {
        "instagram": "feed_image",
        "facebook": "feed_image",
        "threads": "feed_image",
        "x": "feed_image",
        "linkedin": "feed_image",
        "pinterest": "pin",
        "telegram": "channel_post",
        "tiktok": "photo_post",
    },
    "text": {
        "x": "thread",
        "threads": "thread",
        "linkedin": "text_post",
        "facebook": "text_post",
        "telegram": "channel_post",
        "discord": "discussion_question",
        "newsletter": "newsletter_blurb",
        "reddit": "discussion_question",
        "instagram": "carousel",
    },
    "email": {"newsletter": "newsletter_blurb"},
    "article": {"blog": "article", "linkedin": "text_post", "x": "thread", "newsletter": "newsletter_blurb"},
}

ASPECT = {
    "vertical_video": "9:16",
    "feed_image": "4:5",
    "carousel": "4:5",
    "story": "9:16",
    "photo_post": "9:16",
    "pin": "2:3",
    "doc_carousel": "4:5",
    "long_video": "16:9",
}


def format_for(platform: str, mix_kind: str) -> str | None:
    table = FORMAT_FOR_MIX.get(mix_kind, FORMAT_FOR_MIX["text"])
    fmt = table.get(platform)
    if fmt is None:
        spec = PLATFORMS.get(platform)
        return spec.formats[0] if spec else None
    return fmt


def platform_view(p: Platform) -> dict[str, Any]:
    return {
        "key": p.key,
        "label": p.label,
        "kind": p.kind,
        "formats": [{"key": f, "label": FORMAT_LABELS.get(f, f)} for f in p.formats],
        "text_limit": p.text_limit,
        "hashtag_limit": p.hashtag_limit,
        "links_in_text": p.links_in_text,
        "api_posting": p.api_posting,
        "daily_post_limit": p.daily_post_limit,
        "approval_notes": p.approval_notes,
        "ai_label": p.ai_label,
        "best_for": p.best_for,
        "oauth": p.oauth,
        "extra": p.extra,
    }
