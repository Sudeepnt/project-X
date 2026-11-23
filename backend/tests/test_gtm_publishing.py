"""The full loop in practice mode, plus platform adapters against mocked HTTP."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select, text, update

from projectx.kernel.time import utcnow
from projectx.modules.gtm.channels.adapters.base import AdapterContext, AuthExpired, PublishRequest
from projectx.modules.gtm.channels.adapters.messaging import DiscordAdapter, TelegramAdapter
from projectx.modules.gtm.channels.adapters.x import XAdapter
from projectx.modules.gtm.content.importer import parse_feed
from projectx.modules.gtm.models import ContentItem, ContentVariant, NewsletterIssue, PatternStat, Post, Report
from projectx.services import Services
from projectx.spine.decisions.models import Decision
from projectx.spine.jobs.queue import enqueue
from projectx.spine.jobs.worker import Worker


def gtm(business: dict[str, Any], path: str) -> str:
    return f"/api/v1/b/{business['id']}/gtm{path}"


async def _run(services: Services, rounds: int = 6) -> None:
    """Run jobs, fast-forwarding scheduled ones, until nothing is left to do."""
    for _ in range(rounds):
        async with services.db.session() as s:
            await s.execute(
                text(
                    "UPDATE jobs SET run_at = now() WHERE status = 'queued' AND run_at > now() "
                    "AND kind NOT IN ('test.deferred')"
                )
            )
        if await Worker(services).run_until_idle() == 0:
            break


async def test_full_loop_in_practice_mode(services: Services, founder: Any, business: dict[str, Any]) -> None:
    bid = uuid.UUID(business["id"])
    for platform in ("instagram", "x", "telegram"):
        res = await founder.post(gtm(business, "/channels/practice"), json={"platform": platform, "followers": 5000})
        assert res.status_code == 201, res.text
    channels = (await founder.get(gtm(business, "/channels"))).json()
    assert any(p["key"] == "instagram" and p["accounts"] for p in channels["platforms"])

    campaign = (
        await founder.post(
            gtm(business, "/campaigns"),
            json={"playbook_key": "always_on_organic", "start": True, "channels": ["instagram", "x", "telegram"]},
        )
    ).json()
    await _run(services)  # plan → generate → checks → proposals
    async with services.db.tenant(bid) as s:
        items = (
            (await s.execute(select(ContentItem).where(ContentItem.campaign_id == uuid.UUID(campaign["id"]))))
            .scalars()
            .all()
        )
        cards = (
            (
                await s.execute(
                    select(Decision).where(
                        Decision.business_id == bid,
                        Decision.action_type == "gtm.publish_post",
                        Decision.status == "pending",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert items and all(i.status in ("needs_review", "approved", "rejected", "scheduled") for i in items)
    assert cards, "publishing waits for the founder (approve-all mode by default)"
    card = cards[0]
    assert {v["platform"] for v in card.preview["variants"]} <= {"instagram", "x", "telegram"}

    edited_variant = card.preview["variants"][0]["id"]
    res = await founder.post(
        f"/api/v1/b/{bid}/decisions/{card.id}/approve",
        json={"payload_patch": {"edits": {edited_variant: {"text": "Edited by the founder."}}}},
    )
    assert res.status_code == 200, res.text
    await _run(services)  # schedule → publish (simulator) → verify
    async with services.db.tenant(bid) as s:
        posts = (await s.execute(select(Post).where(Post.decision_id == card.id))).scalars().all()
        variant = await s.get(ContentVariant, uuid.UUID(edited_variant))
    assert posts and all(p.status == "published" for p in posts)
    assert all(p.verified_at is not None for p in posts), "verifier reads the post back from the platform"
    assert variant is not None and variant.text == "Edited by the founder."

    async with services.db.tenant(bid) as s:  # pretend two days passed so numbers have matured
        await s.execute(update(Post).where(Post.business_id == bid).values(published_at=utcnow() - timedelta(days=3)))
        await enqueue(s, "gtm.sync_metrics", business_id=bid)
    await _run(services)
    async with services.db.tenant(bid) as s:
        stats = (await s.execute(select(PatternStat).where(PatternStat.business_id == bid))).scalars().all()
    assert {st.dimension for st in stats} >= {"platform", "hook_type", "intent"}

    overview = (await founder.get(gtm(business, "/analytics/overview"))).json()
    assert sum(c["posts"] for c in overview["channels"]) >= len(posts)
    content = (await founder.get(gtm(business, "/analytics/content"))).json()
    assert content["leaderboard"]

    async with services.db.tenant(bid) as s:
        await enqueue(s, "gtm.weekly_report", business_id=bid)
    await _run(services)
    async with services.db.tenant(bid) as s:
        report = await s.scalar(select(Report).where(Report.business_id == bid))
    assert report is not None and "{{" not in report.narrative  # every placeholder filled by code

    cockpit = (await founder.get(f"/api/v1/b/{bid}/cockpit")).json()
    assert "revenue" in cockpit["numbers"] and cockpit["briefing"]


async def test_newsletter_goes_only_to_opted_in(services: Services, founder: Any, business: dict[str, Any]) -> None:
    bid = uuid.UUID(business["id"])
    yes = f"yes-{uuid.uuid4().hex[:6]}@example.com"
    no = f"no-{uuid.uuid4().hex[:6]}@example.com"
    await founder.post(
        gtm(business, "/contacts"),
        json={
            "email": yes,
            "stage": "subscriber",
            "consent": {"channel": "email", "purpose": "newsletter", "evidence": "signup form"},
        },
    )
    await founder.post(gtm(business, "/contacts"), json={"email": no, "stage": "subscriber"})
    async with services.db.tenant(bid) as s:
        item = ContentItem(
            business_id=bid, title="Markets today", body="b", core_idea="c", status="approved", planned_for=utcnow()
        )
        s.add(item)
        await s.flush()
        s.add(
            ContentVariant(
                business_id=bid,
                content_id=item.id,
                platform="newsletter",
                format="newsletter_blurb",
                title="Markets today",
                text="Three things moved markets today.",
                status="ready",
            )
        )
        await enqueue(s, "gtm.build_newsletter", business_id=bid)
    await _run(services)
    async with services.db.tenant(bid) as s:
        issue = await s.scalar(select(NewsletterIssue).where(NewsletterIssue.business_id == bid))
        card = await s.scalar(
            select(Decision).where(Decision.business_id == bid, Decision.action_type == "gtm.send_newsletter")
        )
    assert issue is not None and card is not None and card.status == "pending"
    await founder.post(f"/api/v1/b/{bid}/decisions/{card.id}/approve", json={})
    await _run(services)
    recipients = {m["to"] for m in services.sent_emails if m.get("subject") == "Markets today"}  # type: ignore[attr-defined]
    assert yes in recipients and no not in recipients


async def test_import_and_oauth_not_configured(founder: Any, business: dict[str, Any]) -> None:
    res = await founder.post(
        gtm(business, "/content/import"),
        json={
            "items": [
                {
                    "title": "RBI holds rates",
                    "summary": "The central bank kept rates unchanged.",
                    "url": "https://x.co/a",
                    "external_id": "a1",
                }
            ]
        },
    )
    assert res.json()["created"] == 1
    again = await founder.post(
        gtm(business, "/content/import"),
        json={"items": [{"title": "RBI holds rates", "summary": "dup", "external_id": "a1"}]},
    )
    assert again.json()["created"] == 0  # imports are idempotent
    oauth = await founder.get(gtm(business, "/channels/oauth/instagram/start"))
    assert oauth.status_code == 503 and oauth.json()["code"] == "not_configured"


def test_feed_parsing_is_safe() -> None:
    rss = """<?xml version="1.0"?><rss><channel><item><title>Story one</title><link>https://b.co/1</link>
    <description>Summary one</description><guid>1</guid><pubDate>Mon, 23 Nov 2026 06:00:00 GMT</pubDate></item>
    </channel></rss>"""
    items = parse_feed(rss)
    assert items[0]["title"] == "Story one" and items[0]["published_at"].startswith("2026-11-23")
    bomb = '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]><rss>&lol2;</rss>'
    from defusedxml import EntitiesForbidden

    with pytest.raises(EntitiesForbidden):
        parse_feed(bomb)


def _ctx(http: httpx.AsyncClient, **kw: Any) -> AdapterContext:
    return AdapterContext(
        http=http,
        credentials=kw.get("credentials", {"access_token": "t"}),
        account_external_id="acct",
        settings=kw.get("settings", {}),
        business_id="b",
    )


@respx.mock
async def test_x_adapter_posts_threads_and_maps_auth_errors() -> None:
    calls = respx.post("https://api.x.com/2/tweets").mock(
        side_effect=[httpx.Response(201, json={"data": {"id": "1"}}), httpx.Response(201, json={"data": {"id": "2"}})]
    )
    async with httpx.AsyncClient() as http:
        result = await XAdapter().publish(
            _ctx(http, settings={"handle": "bought"}),
            PublishRequest(platform="x", format="thread", text="Hook", thread=["Part 2"]),
        )
        assert result.external_id == "1" and result.url == "https://x.com/bought/status/1"
        second = calls.calls[1].request
        assert b'"in_reply_to_tweet_id":"1"' in second.content.replace(b" ", b"")
        respx.post("https://api.x.com/2/tweets").mock(return_value=httpx.Response(401, json={"title": "Unauthorized"}))
        try:
            await XAdapter().publish(_ctx(http), PublishRequest(platform="x", format="text_post", text="x"))
            raise AssertionError("expected AuthExpired")
        except AuthExpired:
            pass


@respx.mock
async def test_telegram_and_discord_adapters() -> None:
    respx.post("https://api.telegram.org/botTOKEN/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})
    )
    respx.post("https://discord.com/api/webhooks/1/abc").mock(
        return_value=httpx.Response(200, json={"id": "99", "channel_id": "7"})
    )
    async with httpx.AsyncClient() as http:
        tg = await TelegramAdapter().publish(
            _ctx(http, credentials={"bot_token": "TOKEN"}, settings={"chat_id": "@bought", "username": "bought"}),
            PublishRequest(platform="telegram", format="channel_post", text="Today's digest"),
        )
        assert tg.external_id == "@bought:42" and tg.url == "https://t.me/bought/42"
        dc = await DiscordAdapter().publish(
            _ctx(
                http, credentials={"webhook_url": "https://discord.com/api/webhooks/1/abc"}, settings={"guild_id": "5"}
            ),
            PublishRequest(platform="discord", format="discussion_question", text="Debate of the day?"),
        )
        assert dc.external_id == "99" and dc.url == "https://discord.com/channels/5/7/99"
