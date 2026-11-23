"""Publishing: schedule approved variants, publish once per account, verify by reading back,
respect platform limits, undo on request, and pull metrics for the learning loop."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from projectx.kernel.errors import NotFound
from projectx.kernel.ids import uuid7
from projectx.kernel.time import utcnow
from projectx.modules.gtm.channels.adapters import adapter_for
from projectx.modules.gtm.channels.adapters.base import AdapterContext, AdapterError, AuthExpired, PublishRequest
from projectx.modules.gtm.content.fingerprint import band
from projectx.modules.gtm.learning.patterns import learn_from_post
from projectx.modules.gtm.models import (
    ChannelAccount,
    ContentFingerprint,
    ContentItem,
    ContentVariant,
    Post,
    PostMetricSnapshot,
)
from projectx.modules.gtm.platforms import PLATFORMS
from projectx.services import Services
from projectx.spine.billing.plans import add_usage, remaining
from projectx.spine.decisions.executors import ExecutionContext, ExecutionResult, executor, undoer
from projectx.spine.jobs.queue import enqueue
from projectx.spine.jobs.registry import JobContext, PermanentFailure, RetryLater, job, periodic
from projectx.spine.ledger.service import Actor, append
from projectx.spine.notifications.service import notify
from projectx.spine.policy import autonomy
from projectx.spine.policy.levels import Level
from projectx.spine.tenancy.models import Business

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

AGENT = Actor.agent("pack:gtm.publisher")


async def adapter_context(
    services: Services, session: AsyncSession, account: ChannelAccount, purpose: str
) -> AdapterContext:
    credentials: dict[str, Any] = {}
    if account.secret_id and account.status != "practice":
        credentials = await services.vault.borrow(
            session, account.business_id, account.secret_id, purpose=purpose, actor="pack:gtm.publisher"
        )
    settings = dict(account.settings or {})
    if account.status == "practice":
        business = await session.get(Business, account.business_id)
        settings.setdefault(
            "failure_rate",
            float(((business.settings or {}).get("gtm", {}) if business else {}).get("practice_failure_rate", 0.0)),
        )
    return AdapterContext(
        http=services.http,
        credentials=credentials,
        account_external_id=account.external_id,
        settings=settings,
        business_id=str(account.business_id),
        followers=int(account.followers or 0),
    )


# ------------------------------------------------------------------ scheduling (executor)


@executor("gtm.publish_post")
async def schedule_posts(ctx: ExecutionContext) -> ExecutionResult:
    edits: dict[str, Any] = ctx.payload.get("edits") or {}
    scheduled: list[str] = []
    async with ctx.services.db.tenant(ctx.business_id) as session:
        item = await session.get(ContentItem, uuid.UUID(ctx.payload["content_id"]))
        if item is None:
            raise NotFound("Content not found")
        for variant_id in ctx.payload.get("variant_ids", []):
            variant = await session.get(ContentVariant, uuid.UUID(variant_id))
            if variant is None or variant.account_id is None:
                continue
            if variant_id in edits and isinstance(edits[variant_id], dict) and edits[variant_id].get("text"):
                variant.text = str(edits[variant_id]["text"])
                item.edited_by_human = True
            run_at = max(variant.scheduled_at or utcnow(), utcnow())
            key = f"{variant.id}:{variant.account_id}"
            post_id = await session.scalar(
                insert(Post)
                .values(
                    id=uuid7(),
                    business_id=ctx.business_id,
                    variant_id=variant.id,
                    content_id=item.id,
                    account_id=variant.account_id,
                    campaign_id=item.campaign_id,
                    platform=variant.platform,
                    status="queued",
                    scheduled_at=run_at,
                    idempotency_key=key,
                    decision_id=ctx.decision_id,
                    metrics={},
                )
                .on_conflict_do_nothing(index_elements=[Post.business_id, Post.idempotency_key])
                .returning(Post.id)
            )
            if post_id is None:
                continue
            variant.status = "scheduled"
            await enqueue(
                session,
                "gtm.publish",
                {"post_id": str(post_id)},
                business_id=ctx.business_id,
                run_at=run_at,
                idempotency_key=f"publish:{post_id}",
            )
            await append(
                session,
                business_id=ctx.business_id,
                type="gtm.post.changed",
                actor=AGENT,
                subject=("post", post_id),
                data={
                    "post_id": str(post_id),
                    "platform": variant.platform,
                    "status": "queued",
                    "campaign_id": str(item.campaign_id) if item.campaign_id else None,
                },
            )
            scheduled.append(str(post_id))
        item.status = "scheduled" if scheduled else item.status
        item.approved_at = item.approved_at or utcnow()
    return ExecutionResult(
        data={"post_ids": scheduled}, undo_payload={"post_ids": scheduled}, summary=f"{len(scheduled)} posts scheduled"
    )


@undoer("gtm.publish_post")
async def unpublish(ctx: ExecutionContext) -> dict[str, Any]:
    results: dict[str, str] = {}
    for post_id in (ctx.undo_payload or {}).get("post_ids", []):
        async with ctx.services.db.tenant(ctx.business_id) as session:
            post = await session.get(Post, uuid.UUID(post_id))
            if post is None:
                continue
            if post.status in ("queued", "failed"):
                post.status = "cancelled"
                results[post_id] = "cancelled"
                continue
            account = await session.get(ChannelAccount, post.account_id) if post.account_id else None
            if account is None or post.external_id is None:
                continue
            actx = await adapter_context(ctx.services, session, account, "delete post (undo)")
            adapter = adapter_for(account, ctx.services.settings)
        try:
            await adapter.delete(actx, post.external_id)
            outcome = "deleted"
        except AdapterError as exc:
            outcome = f"not deleted: {exc}"
        async with ctx.services.db.tenant(ctx.business_id) as session:
            fresh = await session.get(Post, uuid.UUID(post_id))
            if fresh is not None and outcome == "deleted":
                fresh.status = "deleted"
                await append(
                    session,
                    business_id=ctx.business_id,
                    type="gtm.post.changed",
                    actor=AGENT,
                    subject=("post", fresh.id),
                    data={"post_id": post_id, "platform": fresh.platform, "status": "deleted"},
                )
        results[post_id] = outcome
    return results


# ------------------------------------------------------------------ publishing (job)


async def _published_last_24h(session: AsyncSession, account_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(Post)
            .where(
                Post.account_id == account_id,
                Post.status == "published",
                Post.published_at > utcnow() - timedelta(hours=24),
            )
        )
        or 0
    )


@job("gtm.publish", max_attempts=6, timeout_s=900, priority=10)
async def publish(ctx: JobContext) -> dict[str, Any] | None:
    business_id = ctx.require_business()
    post_id = uuid.UUID(ctx.payload["post_id"])
    services = ctx.services
    async with services.db.tenant(business_id) as session:
        post = await session.get(Post, post_id)
        if post is None or post.status not in ("queued", "failed"):
            return {"skipped": post.status if post else "missing"}
        variant = await session.get(ContentVariant, post.variant_id)
        account = await session.get(ChannelAccount, post.account_id) if post.account_id else None
        if variant is None or account is None:
            post.status, post.last_error = "failed", "account or content missing"
            raise PermanentFailure("account or content missing")
        if account.status not in ("connected", "practice"):
            post.status, post.last_error = "failed", f"account is {account.status}"
            raise PermanentFailure(f"account is {account.status}")
        if (account.settings or {}).get("paused"):
            raise RetryLater(3600, "account paused by a safety trigger")
        spec = PLATFORMS.get(account.platform)
        if spec and spec.daily_post_limit and await _published_last_24h(session, account.id) >= spec.daily_post_limit:
            raise RetryLater(3600, f"{spec.label} daily limit reached")
        business = await session.get(Business, business_id)
        if business is not None and await remaining(session, business, "posts_per_month") == 0:
            post.status, post.last_error = "failed", "Monthly post limit reached on your plan"
            await notify(
                session,
                business_id,
                kind="billing.limit_reached",
                level="warning",
                title="Monthly post limit reached",
                body="Posts pause until next month, or upgrade to keep publishing.",
                link="/settings?tab=plan",
            )
            raise PermanentFailure("monthly post limit reached")
        post.status = "publishing"
        post.attempts += 1
        actx = await adapter_context(services, session, account, f"publish post {post.id}")
        adapter = adapter_for(account, services.settings)
        media = [
            {
                "url": services.blobs.signed_url(m["key"], ttl_s=86400) if m.get("key") else m.get("url"),
                "kind": m.get("kind", "image"),
            }
            for m in (variant.media or [])
        ]
        request = PublishRequest(
            platform=variant.platform,
            format=variant.format,
            text=variant.text,
            title=variant.title,
            thread=[str(t) for t in (variant.thread or [])],
            media=media,
            ai_label=(variant.spec or {}).get("ai_label", "none"),
            idempotency_key=post.idempotency_key,
        )
        item_simhash = (await session.get(ContentItem, post.content_id)).simhash  # type: ignore[union-attr]
    try:
        result = await adapter.publish(actx, request)
    except AuthExpired as exc:
        async with services.db.tenant(business_id) as session:
            post = await session.get(Post, post_id)
            acct = await session.get(ChannelAccount, account.id)
            if post is not None:
                post.status, post.last_error = "failed", str(exc)
            if acct is not None:
                acct.status, acct.last_error = "expired", str(exc)
                await notify(
                    session,
                    business_id,
                    kind="account.expired",
                    level="critical",
                    title=f"Reconnect {acct.name}",
                    body="Posting stopped until the account is reconnected.",
                    link="/channels",
                )
        raise PermanentFailure(str(exc)) from exc
    except AdapterError as exc:
        async with services.db.tenant(business_id) as session:
            post = await session.get(Post, post_id)
            if post is not None:
                post.status, post.last_error = "failed", str(exc)[:2000]
        if not exc.retryable:
            await _incident(services, business_id, post_id, str(exc))
            raise PermanentFailure(str(exc)) from exc
        if exc.retry_after:
            raise RetryLater(exc.retry_after, str(exc)) from exc
        raise

    async with services.db.tenant(business_id) as session:
        post = await session.get(Post, post_id)
        assert post is not None
        post.status, post.external_id, post.url, post.published_at = (
            "published",
            result.external_id,
            result.url,
            utcnow(),
        )
        post.last_error = None
        variant = await session.get(ContentVariant, post.variant_id)
        if variant is not None:
            variant.status = "published"
        item = await session.get(ContentItem, post.content_id)
        if item is not None:
            item.status = "published"
        if item_simhash is not None:
            session.add(
                ContentFingerprint(
                    business_id=business_id, simhash=item_simhash, band=band(item_simhash), platform=post.platform
                )
            )
        await add_usage(session, business_id, "posts_per_month")
        await append(
            session,
            business_id=business_id,
            type="gtm.post.changed",
            actor=AGENT,
            subject=("post", post.id),
            data={
                "post_id": str(post.id),
                "platform": post.platform,
                "status": "published",
                "external_ref": result.external_id[:200],
                "campaign_id": str(post.campaign_id) if post.campaign_id else None,
            },
        )
        await enqueue(
            session,
            "gtm.verify_post",
            {"post_id": str(post.id)},
            business_id=business_id,
            run_at=utcnow() + timedelta(seconds=60),
            idempotency_key=f"verify:{post.id}",
        )
    return {"published": result.external_id}


async def _incident(services: Services, business_id: uuid.UUID, post_id: uuid.UUID, detail: str) -> None:
    async with services.db.tenant(business_id) as session:
        await autonomy.demote(
            session,
            business_id,
            "gtm.publish_post",
            reason=f"incident: {detail[:120]}",
            floor=Level.ASK_FIRST,
            incident=True,
        )
        await append(
            session,
            business_id=business_id,
            type="gtm.rule.fired",
            actor=AGENT,
            data={"rule": "publish_refused", "scope": "post", "scope_id": str(post_id), "detail": detail[:300]},
        )
        await notify(
            session,
            business_id,
            kind="post.failed",
            level="warning",
            title="A post was refused",
            body=detail[:300],
            link="/content",
        )


@job("gtm.verify_post", max_attempts=4, priority=30)
async def verify_post(ctx: JobContext) -> dict[str, Any] | None:
    """The verifier reads the outcome from the platform, never from our own publish call."""
    business_id = ctx.require_business()
    post_id = uuid.UUID(ctx.payload["post_id"])
    async with ctx.services.db.tenant(business_id) as session:
        post = await session.get(Post, post_id)
        if post is None or post.status != "published" or post.external_id is None:
            return None
        account = await session.get(ChannelAccount, post.account_id) if post.account_id else None
        if account is None:
            return None
        actx = await adapter_context(ctx.services, session, account, f"verify post {post.id}")
        adapter = adapter_for(account, ctx.services.settings)
        external_id = post.external_id
    live = await adapter.verify(actx, external_id)
    async with ctx.services.db.tenant(business_id) as session:
        post = await session.get(Post, post_id)
        assert post is not None
        if live:
            post.verified_at = utcnow()
            await append(
                session,
                business_id=business_id,
                type="gtm.post.changed",
                actor=Actor.agent("verifier"),
                subject=("post", post.id),
                data={"post_id": str(post.id), "platform": post.platform, "status": "verified"},
            )
            return {"verified": True}
    if ctx.attempt < 3:
        raise RetryLater(300, "post not visible yet")
    async with ctx.services.db.tenant(business_id) as session:
        post = await session.get(Post, post_id)
        if post is not None:
            post.status, post.last_error = "failed", "published but not visible on the platform"
    await _incident(ctx.services, business_id, post_id, "A published post could not be found on the platform")
    return {"verified": False}


# ------------------------------------------------------------------ manual posts and metrics


async def record_manual_post(
    services: Services, business_id: uuid.UUID, variant_id: uuid.UUID, url: str | None
) -> None:
    async with services.db.tenant(business_id) as session:
        variant = await session.get(ContentVariant, variant_id)
        if variant is None:
            return
        item = await session.get(ContentItem, variant.content_id)
        key = f"{variant.id}:manual"
        exists = await session.scalar(
            select(Post.id).where(Post.business_id == business_id, Post.idempotency_key == key)
        )
        if exists:
            return
        post = Post(
            business_id=business_id,
            variant_id=variant.id,
            content_id=variant.content_id,
            account_id=None,
            campaign_id=item.campaign_id if item else None,
            platform=variant.platform,
            status="published",
            url=url,
            published_at=utcnow(),
            verified_at=utcnow() if url else None,
            idempotency_key=key,
            metrics={},
        )
        session.add(post)
        variant.status = "published"
        await session.flush()
        await append(
            session,
            business_id=business_id,
            type="gtm.post.changed",
            actor=Actor.system("manual"),
            subject=("post", post.id),
            data={"post_id": str(post.id), "platform": post.platform, "status": "published"},
        )


@job("gtm.sync_metrics", max_attempts=3, timeout_s=900, priority=70)
async def sync_metrics(ctx: JobContext) -> dict[str, Any] | None:
    business_id = ctx.require_business()
    services = ctx.services
    updated = learned = 0
    async with services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        accounts = (
            (
                await session.execute(
                    select(ChannelAccount).where(
                        ChannelAccount.business_id == business_id, ChannelAccount.status.in_(("connected", "practice"))
                    )
                )
            )
            .scalars()
            .all()
        )
        plan: list[tuple[ChannelAccount, AdapterContext, list[tuple[uuid.UUID, str]]]] = []
        for account in accounts:
            posts = (
                await session.execute(
                    select(Post.id, Post.external_id).where(
                        Post.account_id == account.id,
                        Post.status == "published",
                        Post.external_id.is_not(None),
                        Post.published_at > utcnow() - timedelta(days=30),
                    )
                )
            ).all()
            if posts:
                actx = await adapter_context(services, session, account, "read post metrics")
                plan.append((account, actx, [(pid, str(ext)) for pid, ext in posts]))
        tz = business.timezone if business else "UTC"
    for account, actx, targets in plan:
        adapter = adapter_for(account, services.settings)
        try:
            numbers = await adapter.metrics(actx, [ext for _, ext in targets])
        except AdapterError as exc:
            ctx.log.warning("metrics.failed", platform=account.platform, error=str(exc))
            continue
        async with services.db.tenant(business_id) as session:
            for post_id, external_id in targets:
                values = numbers.get(external_id)
                if not values:
                    continue
                post = await session.get(Post, post_id)
                if post is None:
                    continue
                post.metrics, post.metrics_at = values, utcnow()
                session.add(PostMetricSnapshot(business_id=business_id, post_id=post_id, metrics=values))
                updated += 1
                matured = post.published_at is not None and post.published_at < utcnow() - timedelta(hours=48)
                if matured and await learn_from_post(session, post, tz) is not None:
                    learned += 1
    return {"updated": updated, "learned": learned}


@periodic("gtm.schedule_metrics", every=timedelta(hours=6))
async def schedule_metrics(services: Services, now: datetime) -> None:
    window = now.strftime("%Y%m%d") + str(now.hour // 6)
    async with services.db.session() as session:
        ids = (await session.execute(select(Business.id).where(Business.status == "active"))).scalars().all()
        for business_id in ids:
            await enqueue(
                session, "gtm.sync_metrics", business_id=business_id, idempotency_key=f"metrics:{business_id}:{window}"
            )


@executor("gtm.pause_account")
async def pause_account(ctx: ExecutionContext) -> ExecutionResult:
    async with ctx.services.db.tenant(ctx.business_id) as session:
        account = await session.get(ChannelAccount, uuid.UUID(ctx.payload["account_id"]))
        if account is None:
            raise NotFound("Account not found")
        account.settings = {**(account.settings or {}), "paused": True, "paused_reason": ctx.payload.get("reason")}
    return ExecutionResult(
        data={"account_id": ctx.payload["account_id"]},
        summary="Posting paused",
        undo_payload={"account_id": ctx.payload["account_id"]},
    )


@undoer("gtm.pause_account")
async def resume_account(ctx: ExecutionContext) -> dict[str, Any]:
    async with ctx.services.db.tenant(ctx.business_id) as session:
        account = await session.get(ChannelAccount, uuid.UUID((ctx.undo_payload or {})["account_id"]))
        if account is not None:
            account.settings = {
                k: v for k, v in (account.settings or {}).items() if k not in ("paused", "paused_reason")
            }
    return {"resumed": True}


def post_view(p: Post) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "variant_id": str(p.variant_id),
        "content_id": str(p.content_id),
        "account_id": str(p.account_id) if p.account_id else None,
        "platform": p.platform,
        "status": p.status,
        "external_id": p.external_id,
        "url": p.url,
        "scheduled_at": p.scheduled_at.isoformat() if p.scheduled_at else None,
        "published_at": p.published_at.isoformat() if p.published_at else None,
        "verified_at": p.verified_at.isoformat() if p.verified_at else None,
        "attempts": p.attempts,
        "last_error": p.last_error,
        "metrics": p.metrics,
        "score": p.score,
        "campaign_id": str(p.campaign_id) if p.campaign_id else None,
    }
