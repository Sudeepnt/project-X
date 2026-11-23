"""The ads engine: put money behind what already works, inside hard caps, and stop what doesn't.

Money is capped in two places:
  1. Our own monthly cap per ad account. Every new campaign, budget increase and resume is checked
     against "spent this month + committed daily budgets for the rest of the month", and the check
     runs again at execution time (things change between proposal and approval).
  2. The platform's own account spending limit, mirrored from our cap where the platform has one
     (Meta). Where it doesn't (Google), the daily job pauses campaigns the moment the cap is reached.

Starting ads and raising budgets always need a person (the action ceiling is Ask first). Pausing
can run on its own, because it only ever stops spending.
"""

from __future__ import annotations

import calendar
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.errors import Conflict, NotFound, PolicyBlocked, ValidationFailed
from projectx.kernel.time import today_in, utcnow
from projectx.modules.gtm.ads.adapters import (
    REQUIRED_CREDENTIALS,
    AdsContext,
    AdSpec,
    DayMetrics,
    adapter_for,
)
from projectx.modules.gtm.channels.adapters.base import AdapterError, AuthExpired
from projectx.modules.gtm.goals import restrictions as restrictions_svc
from projectx.modules.gtm.models import AdAccount, AdCampaign, AdMetricDaily, ContentItem
from projectx.services import Services
from projectx.spine.decisions.executors import ExecutionContext, ExecutionResult, executor, undoer
from projectx.spine.decisions.service import Proposal, ProposalOutcome, propose
from projectx.spine.jobs.registry import JobContext, RetryLater, job
from projectx.spine.ledger.service import Actor, append
from projectx.spine.notifications.service import notify
from projectx.spine.policy.engine import Violation
from projectx.spine.tenancy.models import Business

AGENT = Actor.agent("pack:gtm.ads")

OBJECTIVES = ("leads", "traffic", "sales")
AUDIENCES = ("retargeting", "lookalike", "cold")
SPECIAL_CATEGORIES = ("housing", "employment", "credit", "politics")
ACTIVE = ("active",)

# Stop rules (blueprint): CPL above 2x target for 3 days, or real spend with zero results.
CPL_MULTIPLE = 2.0
CPL_DAYS = 3
ZERO_RESULT_BUDGET_DAYS = 3  # three days of budget spent and not one lead


# ---------------------------------------------------------------- money arithmetic


def days_left_in_month(today: date) -> int:
    return calendar.monthrange(today.year, today.month)[1] - today.day + 1


async def month_spend(session: AsyncSession, account_id: uuid.UUID, today: date) -> int:
    start = today.replace(day=1)
    total = await session.scalar(
        select(func.coalesce(func.sum(AdMetricDaily.spend_minor), 0))
        .join(AdCampaign, AdCampaign.id == AdMetricDaily.ad_campaign_id)
        .where(AdCampaign.ad_account_id == account_id, AdMetricDaily.day >= start, AdMetricDaily.day <= today)
    )
    return int(total or 0)


async def committed_daily(session: AsyncSession, account_id: uuid.UUID, *, exclude: uuid.UUID | None = None) -> int:
    stmt = select(func.coalesce(func.sum(AdCampaign.daily_budget_minor), 0)).where(
        AdCampaign.ad_account_id == account_id, AdCampaign.status.in_(ACTIVE)
    )
    if exclude is not None:
        stmt = stmt.where(AdCampaign.id != exclude)
    return int(await session.scalar(stmt) or 0)


@dataclass(frozen=True)
class CapCheck:
    spent: int
    projected: int
    cap: int
    violations: tuple[Violation, ...]

    def forecast(self, currency: str) -> dict[str, Any]:
        return {
            "spent_this_month_minor": self.spent,
            "projected_month_minor": self.projected,
            "monthly_cap_minor": self.cap,
            "currency": currency,
        }


async def check_caps(
    session: AsyncSession,
    business: Business,
    account: AdAccount,
    *,
    new_daily_minor: int,
    replacing: uuid.UUID | None = None,
) -> CapCheck:
    """Would running `new_daily_minor` a day (on top of every other active campaign) stay inside the caps?"""
    today = today_in(business.timezone)
    spent = await month_spend(session, account.id, today)
    daily = await committed_daily(session, account.id, exclude=replacing) + new_daily_minor
    projected = spent + daily * days_left_in_month(today)
    violations: list[Violation] = []
    if account.monthly_cap_minor <= 0:
        violations.append(Violation("no_ad_cap", "Set a monthly ad cap for this account first."))
    elif projected > account.monthly_cap_minor:
        violations.append(
            Violation(
                "over_monthly_ad_cap",
                f"This would spend about {projected / 100:,.0f} {account.currency} this month, "
                f"over the {account.monthly_cap_minor / 100:,.0f} {account.currency} cap.",
            )
        )
    for r in await restrictions_svc.active(session, business.id):
        amount = int(r.params.get("amount_minor") or 0)
        if r.kind == "daily_budget" and amount and daily > amount:
            violations.append(
                Violation("over_daily_budget_rule", f"Daily ad spend would pass the {amount / 100:,.0f} limit.")
            )
        if r.kind == "total_budget" and amount and projected > amount:
            violations.append(
                Violation("over_total_budget_rule", f"Spend would pass the {amount / 100:,.0f} goal budget.")
            )
    return CapCheck(spent=spent, projected=projected, cap=account.monthly_cap_minor, violations=tuple(violations))


async def target_cpl(session: AsyncSession, business: Business, campaign: AdCampaign) -> int | None:
    if campaign.target_cpl_minor:
        return campaign.target_cpl_minor
    for r in await restrictions_svc.active(session, business.id):
        if r.kind == "cpl_cap" and r.params.get("amount_minor"):
            return int(r.params["amount_minor"])
    return None


# ---------------------------------------------------------------- accounts


async def connect_practice(
    session: AsyncSession, business: Business, *, platform: str, monthly_cap_minor: int
) -> AdAccount:
    if platform not in REQUIRED_CREDENTIALS:
        raise ValidationFailed("Ads run on Meta or Google")
    return await _save_account(
        session,
        business,
        platform=platform,
        external_id=f"practice-{platform}",
        name=f"Practice {'Meta' if platform == 'meta' else 'Google'} ads",
        status="practice",
        secret_id=None,
        monthly_cap_minor=monthly_cap_minor,
        settings={"provider": "simulator"},
    )


async def connect_token(
    session: AsyncSession,
    services: Services,
    business: Business,
    *,
    platform: str,
    external_id: str,
    name: str,
    credentials: dict[str, Any],
    settings: dict[str, Any],
    monthly_cap_minor: int,
) -> AdAccount:
    required = REQUIRED_CREDENTIALS.get(platform)
    if required is None:
        raise ValidationFailed("Ads run on Meta or Google")
    missing = [k for k in required if not credentials.get(k)]
    if missing:
        raise ValidationFailed(f"Missing {', '.join(missing)}")
    if platform == "google" and not external_id.replace("-", "").isdigit():
        raise ValidationFailed("Google Ads customer IDs look like 123-456-7890")
    secret_id = await services.vault.put(
        session, business.id, kind="api_key", label=f"ads:{platform}:{external_id}", secret=credentials
    )
    account = await _save_account(
        session,
        business,
        platform=platform,
        external_id=external_id,
        name=name,
        status="connected",
        secret_id=secret_id,
        monthly_cap_minor=monthly_cap_minor,
        settings=settings,
    )
    await mirror_cap(session, services, account)
    return account


async def _save_account(
    session: AsyncSession,
    business: Business,
    *,
    platform: str,
    external_id: str,
    name: str,
    status: str,
    secret_id: uuid.UUID | None,
    monthly_cap_minor: int,
    settings: dict[str, Any],
) -> AdAccount:
    if monthly_cap_minor < 0:
        raise ValidationFailed("The cap can't be negative")
    account = await session.scalar(
        select(AdAccount).where(
            AdAccount.business_id == business.id, AdAccount.platform == platform, AdAccount.external_id == external_id
        )
    )
    if account is None:
        account = AdAccount(
            business_id=business.id, platform=platform, external_id=external_id, currency=business.currency
        )
        session.add(account)
    account.name, account.status, account.monthly_cap_minor, account.settings = (
        name,
        status,
        monthly_cap_minor,
        settings,
    )
    if secret_id is not None:
        account.secret_id = secret_id
    if status == "practice":  # the simulator honours a spending limit, like Meta does
        account.platform_spend_limit_minor = monthly_cap_minor
    await session.flush()
    await append(
        session,
        business_id=business.id,
        type="gtm.ad.changed",
        actor=AGENT,
        subject=("ad_account", account.id),
        data={
            "ad_campaign_id": str(account.id),
            "platform": platform,
            "status": f"account_{status}",
            "daily_budget_minor": None,
            "reason": f"monthly cap {monthly_cap_minor}",
        },
    )
    return account


async def mirror_cap(session: AsyncSession, services: Services, account: AdAccount) -> None:
    """Copy our monthly cap to the platform's own spending limit, where one exists."""
    ctx = await context_for(session, services, account, purpose="mirror ad cap")
    try:
        supported = await adapter_for(account.platform, account.status == "practice").set_spend_limit(
            ctx, account.monthly_cap_minor or None
        )
    except AdapterError as exc:
        account.settings = {**(account.settings or {}), "cap_mirror_error": str(exc)[:300]}
        return
    account.platform_spend_limit_minor = account.monthly_cap_minor if supported else None
    account.settings = {k: v for k, v in (account.settings or {}).items() if k != "cap_mirror_error"}


async def context_for(
    session: AsyncSession, services: Services, account: AdAccount, *, purpose: str, campaign: AdCampaign | None = None
) -> AdsContext:
    credentials: dict[str, Any] = {}
    if account.secret_id and account.status != "practice":
        credentials = await services.vault.borrow(
            session, account.business_id, account.secret_id, purpose=purpose, actor="pack:gtm.ads"
        )
        if account.platform == "google" and credentials.get("refresh_token"):
            credentials = {**credentials, "access_token": await _google_access_token(services, credentials)}
    settings = dict(account.settings or {})
    if campaign is not None:
        settings["daily_budget_minor"] = campaign.daily_budget_minor
        settings.update((campaign.rules or {}).get("simulation", {}))
    return AdsContext(
        http=services.http,
        credentials=credentials,
        account_external_id=account.external_id,
        settings=settings,
        currency=account.currency,
    )


async def _google_access_token(services: Services, credentials: dict[str, Any]) -> str:
    cfg = services.settings
    client_id = credentials.get("client_id") or cfg.google_client_id
    secret = credentials.get("client_secret") or (
        cfg.google_client_secret.get_secret_value() if cfg.google_client_secret else None
    )
    if not client_id or not secret:
        raise AuthExpired("Google Ads needs OAuth client credentials to refresh its token")
    response = await services.http.post(
        "https://oauth2.googleapis.com/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": credentials["refresh_token"],
            "client_id": client_id,
            "client_secret": secret,
        },
    )
    if response.status_code >= 400:
        raise AuthExpired("Google refused to refresh the Ads token; reconnect the account")
    return str(response.json()["access_token"])


# ---------------------------------------------------------------- proposals


def validate_targeting(special_category: str | None, audience_kind: str) -> None:
    if special_category == "politics":
        raise PolicyBlocked(
            "Political and issue ads need platform authorisation and 'paid for by' disclaimers. "
            "Run them yourself in the ad platform; they're never automated here."
        )
    if special_category and special_category not in SPECIAL_CATEGORIES:
        raise ValidationFailed("Unknown special category")
    if special_category and audience_kind == "lookalike":
        raise ValidationFailed(
            "Housing, employment and credit ads can't use lookalike audiences; platforms restrict targeting by law."
        )
    if audience_kind not in AUDIENCES:
        raise ValidationFailed("Unknown audience")


async def propose_campaign(
    session: AsyncSession,
    business: Business,
    *,
    account: AdAccount,
    name: str,
    objective: str,
    daily_budget_minor: int,
    audience_kind: str,
    special_category: str | None,
    content_ids: list[uuid.UUID],
    landing_url: str | None,
    target_cpl_minor: int | None,
    campaign_id: uuid.UUID | None,
    external_campaign_id: str | None,
    ends_on: date | None,
    requested_by: str,
) -> tuple[AdCampaign, ProposalOutcome]:
    if objective not in OBJECTIVES:
        raise ValidationFailed("Objective must be leads, traffic or sales")
    if daily_budget_minor <= 0:
        raise ValidationFailed("Set a daily budget above zero")
    validate_targeting(special_category, audience_kind)
    if account.status not in ("connected", "practice"):
        raise Conflict("Reconnect this ad account first")
    creatives = []
    for cid in content_ids:
        item = await session.get(ContentItem, cid)
        if item is None:
            raise NotFound("Content not found")
        creatives.append(item)
    caps = await check_caps(session, business, account, new_daily_minor=daily_budget_minor)
    ad = AdCampaign(
        business_id=business.id,
        ad_account_id=account.id,
        campaign_id=campaign_id,
        platform=account.platform,
        external_id=external_campaign_id if account.platform == "google" else None,
        name=name[:200],
        objective=objective,
        audience_kind=audience_kind,
        daily_budget_minor=daily_budget_minor,
        status="pending_approval",
        special_category=special_category,
        creative_content_ids=[str(c) for c in content_ids],
        rules={"landing_url": landing_url},
        target_cpl_minor=target_cpl_minor,
        starts_on=today_in(business.timezone),
        ends_on=ends_on,
    )
    session.add(ad)
    await session.flush()
    money = f"{daily_budget_minor / 100:,.2f} {account.currency}"
    outcome = await propose(
        session,
        business.id,
        Proposal(
            action_type="gtm.create_ad_campaign",
            title=f"Start ads: {ad.name} at {money} a day",
            summary=f"{objective.title()} on {account.platform.title()}, {audience_kind} audience"
            + (f", {len(creatives)} proven post{'s' if len(creatives) != 1 else ''}" if creatives else ""),
            payload={"ad_campaign_id": str(ad.id)},
            preview={
                "creatives": [{"id": str(c.id), "title": c.title, "body": c.body[:400]} for c in creatives],
                "audience": audience_kind,
                "special_category": special_category,
                "landing_url": landing_url,
                "target_cpl_minor": target_cpl_minor,
            },
            amount_minor=daily_budget_minor,
            currency=account.currency,
            violations=caps.violations,
            forecast=caps.forecast(account.currency),
            why_now="These posts already worked without ads; a small budget tests whether they scale.",
            if_no="Nothing is spent. Organic posting continues.",
            undo="Pause the campaign; money already spent can't be returned.",
            subject=("ad_campaign", ad.id),
            requested_by=requested_by,
            priority=20,
        ),
    )
    if outcome.verdict.blocked:
        ad.status, ad.paused_reason = "blocked", "; ".join(v.message for v in caps.violations) or "Blocked by policy"
    return ad, outcome


async def propose_budget(
    session: AsyncSession,
    business: Business,
    ad: AdCampaign,
    *,
    daily_budget_minor: int,
    requested_by: str,
    services: Services,
) -> ProposalOutcome | None:
    """Lowering a budget happens at once (it only reduces spend). Raising it always asks."""
    if daily_budget_minor <= 0:
        raise ValidationFailed("Pause the campaign instead of setting a zero budget")
    account = await _account(session, ad)
    if daily_budget_minor <= ad.daily_budget_minor:
        if ad.status in ACTIVE and ad.external_id:
            await adapter_for(account.platform, account.status == "practice").set_daily_budget(
                await context_for(session, services, account, purpose="lower ad budget", campaign=ad),
                ad.external_id,
                daily_budget_minor,
            )
        ad.daily_budget_minor = daily_budget_minor
        await _ledger(
            session, ad, "budget_lowered", Actor.agent(requested_by) if requested_by.startswith("pack:") else AGENT
        )
        return None
    caps = await check_caps(session, business, account, new_daily_minor=daily_budget_minor, replacing=ad.id)
    return await propose(
        session,
        business.id,
        Proposal(
            action_type="gtm.increase_ad_budget",
            title=f"Raise {ad.name} to {daily_budget_minor / 100:,.2f} {account.currency} a day",
            summary=f"From {ad.daily_budget_minor / 100:,.2f} {account.currency} a day.",
            payload={"ad_campaign_id": str(ad.id), "daily_budget_minor": daily_budget_minor},
            amount_minor=daily_budget_minor - ad.daily_budget_minor,
            currency=account.currency,
            violations=caps.violations,
            forecast=caps.forecast(account.currency),
            if_no="The budget stays as it is.",
            subject=("ad_campaign", ad.id),
            requested_by=requested_by,
            priority=25,
        ),
    )


async def propose_pause(
    session: AsyncSession, business: Business, ad: AdCampaign, *, reason: str, requested_by: str
) -> ProposalOutcome:
    return await propose(
        session,
        business.id,
        Proposal(
            action_type="gtm.pause_ad",
            title=f"Pause {ad.name}",
            summary=reason,
            payload={"ad_campaign_id": str(ad.id), "reason": reason},
            why_now=reason,
            if_no="It keeps spending at the current budget.",
            undo="Resume the ad.",
            subject=("ad_campaign", ad.id),
            requested_by=requested_by,
            priority=15,
        ),
    )


async def resume(
    session: AsyncSession, services: Services, business: Business, ad: AdCampaign, *, actor: Actor
) -> AdCampaign:
    """The founder's own click. Still checked against every cap before money flows again."""
    if ad.status != "paused":
        raise Conflict(f"This campaign is {ad.status}")
    account = await _account(session, ad)
    caps = await check_caps(session, business, account, new_daily_minor=ad.daily_budget_minor, replacing=ad.id)
    if caps.violations:
        raise PolicyBlocked(caps.violations[0].message)
    if ad.external_id:
        await adapter_for(account.platform, account.status == "practice").set_status(
            await context_for(session, services, account, purpose="resume ad", campaign=ad), ad.external_id, True
        )
    ad.status, ad.paused_reason = "active", None
    await _ledger(session, ad, "resumed", actor)
    return ad


# ---------------------------------------------------------------- executors


async def _account(session: AsyncSession, ad: AdCampaign) -> AdAccount:
    account = await session.get(AdAccount, ad.ad_account_id)
    if account is None:
        raise NotFound("Ad account not found")
    return account


async def _ledger(session: AsyncSession, ad: AdCampaign, status: str, actor: Actor, reason: str | None = None) -> None:
    await append(
        session,
        business_id=ad.business_id,
        type="gtm.ad.changed",
        actor=actor,
        subject=("ad_campaign", ad.id),
        data={
            "ad_campaign_id": str(ad.id),
            "platform": ad.platform,
            "status": status,
            "daily_budget_minor": ad.daily_budget_minor,
            "reason": reason,
        },
    )


async def _load(ctx: ExecutionContext, session: AsyncSession) -> tuple[Business, AdCampaign, AdAccount]:
    business = await session.get(Business, ctx.business_id)
    ad = await session.get(AdCampaign, uuid.UUID(ctx.payload["ad_campaign_id"]))
    if business is None or ad is None:
        raise NotFound("Ad campaign not found")
    return business, ad, await _account(session, ad)


@executor("gtm.create_ad_campaign")
async def _create(ctx: ExecutionContext) -> ExecutionResult:
    async with ctx.services.db.tenant(ctx.business_id) as session:
        business, ad, account = await _load(ctx, session)
        if ad.status == "active" and ad.external_id:
            return ExecutionResult(data={"already": True, "external_id": ad.external_id})
        caps = await check_caps(session, business, account, new_daily_minor=ad.daily_budget_minor, replacing=ad.id)
        if caps.violations:  # re-checked at execution time
            ad.status, ad.paused_reason = "blocked", caps.violations[0].message
            raise PolicyBlocked(caps.violations[0].message)
        creative = None
        if ad.creative_content_ids:
            creative = await session.get(ContentItem, uuid.UUID(ad.creative_content_ids[0]))
        spec = AdSpec(
            name=ad.name,
            objective=ad.objective,
            daily_budget_minor=ad.daily_budget_minor,
            audience_kind=ad.audience_kind,
            special_category=ad.special_category,
            country=business.country,
            landing_url=(ad.rules or {}).get("landing_url"),
            message=creative.body if creative else ad.name,
            headline=creative.title if creative else ad.name,
            external_campaign_id=ad.external_id,
            idempotency_key=str(ad.id),
        )
        adapter_ctx = await context_for(session, ctx.services, account, purpose="create ad campaign", campaign=ad)
    created = await adapter_for(account.platform, account.status == "practice").create(adapter_ctx, spec)
    async with ctx.services.db.tenant(ctx.business_id) as session:
        fresh = await session.get(AdCampaign, uuid.UUID(ctx.payload["ad_campaign_id"]))
        assert fresh is not None
        fresh.external_id, fresh.status, fresh.paused_reason = created.external_id, "active", None
        fresh.rules = {**(fresh.rules or {}), "platform": created.extra}
        await _ledger(session, fresh, "active", AGENT)
    return ExecutionResult(
        data={"external_id": created.external_id},
        external_ref=created.external_id,
        undo_payload={"ad_campaign_id": ctx.payload["ad_campaign_id"]},
        amount_minor=ad.daily_budget_minor,
    )


@undoer("gtm.create_ad_campaign")
async def _undo_create(ctx: ExecutionContext) -> dict[str, Any]:
    return await _pause_now(
        ctx.services,
        ctx.business_id,
        uuid.UUID((ctx.undo_payload or {})["ad_campaign_id"]),
        "Undone by founder",
        Actor.system("undo"),
    )


@executor("gtm.increase_ad_budget")
async def _increase(ctx: ExecutionContext) -> ExecutionResult:
    new_budget = int(ctx.payload["daily_budget_minor"])
    async with ctx.services.db.tenant(ctx.business_id) as session:
        business, ad, account = await _load(ctx, session)
        caps = await check_caps(session, business, account, new_daily_minor=new_budget, replacing=ad.id)
        if caps.violations:
            raise PolicyBlocked(caps.violations[0].message)
        previous = ad.daily_budget_minor
        if ad.status in ACTIVE and ad.external_id:
            await adapter_for(account.platform, account.status == "practice").set_daily_budget(
                await context_for(session, ctx.services, account, purpose="raise ad budget", campaign=ad),
                ad.external_id,
                new_budget,
            )
        ad.daily_budget_minor = new_budget
        await _ledger(session, ad, "budget_raised", AGENT)
    return ExecutionResult(data={"from": previous, "to": new_budget}, amount_minor=new_budget - previous)


@executor("gtm.pause_ad")
async def _pause(ctx: ExecutionContext) -> ExecutionResult:
    result = await _pause_now(
        ctx.services,
        ctx.business_id,
        uuid.UUID(ctx.payload["ad_campaign_id"]),
        ctx.payload.get("reason") or "Paused",
        AGENT,
    )
    return ExecutionResult(data=result, undo_payload={"ad_campaign_id": ctx.payload["ad_campaign_id"]})


@undoer("gtm.pause_ad")
async def _undo_pause(ctx: ExecutionContext) -> dict[str, Any]:
    async with ctx.services.db.tenant(ctx.business_id) as session:
        business = await session.get(Business, ctx.business_id)
        ad = await session.get(AdCampaign, uuid.UUID((ctx.undo_payload or {})["ad_campaign_id"]))
        if business is None or ad is None:
            raise NotFound("Ad campaign not found")
        await resume(session, ctx.services, business, ad, actor=Actor.system("undo"))
    return {"resumed": True}


async def _pause_now(
    services: Services, business_id: uuid.UUID, ad_id: uuid.UUID, reason: str, actor: Actor
) -> dict[str, Any]:
    async with services.db.tenant(business_id) as session:
        ad = await session.get(AdCampaign, ad_id)
        if ad is None:
            raise NotFound("Ad campaign not found")
        return await pause_in(session, services, ad, reason, actor)


async def pause_in(
    session: AsyncSession, services: Services, ad: AdCampaign, reason: str, actor: Actor
) -> dict[str, Any]:
    """Stop spending now, inside the caller's transaction."""
    if ad.status != "active":
        return {"skipped": ad.status}
    account = await _account(session, ad)
    if ad.external_id:
        await adapter_for(account.platform, account.status == "practice").set_status(
            await context_for(session, services, account, purpose="pause ad", campaign=ad), ad.external_id, False
        )
    ad.status, ad.paused_reason = "paused", reason[:500]
    await _ledger(session, ad, "paused", actor, reason[:200])
    return {"paused": True}


# ---------------------------------------------------------------- daily sync and stop rules


def _cpl_breach(days: list[AdMetricDaily], target: int) -> bool:
    recent = sorted(days, key=lambda d: d.day)[-CPL_DAYS:]
    if len(recent) < CPL_DAYS:
        return False
    return all(d.spend_minor > 0 and (d.leads == 0 or d.spend_minor / d.leads > CPL_MULTIPLE * target) for d in recent)


@job("gtm.ads_daily", max_attempts=5, timeout_s=300, priority=40)
async def ads_daily(ctx: JobContext) -> dict[str, Any]:
    business_id = ctx.require_business()
    synced = paused = proposed = 0
    async with ctx.services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        assert business is not None
        today = today_in(business.timezone)
        accounts = (
            (await session.execute(select(AdAccount).where(AdAccount.business_id == business_id))).scalars().all()
        )
        for account in accounts:
            ads = (
                (
                    await session.execute(
                        select(AdCampaign).where(AdCampaign.ad_account_id == account.id, AdCampaign.status.in_(ACTIVE))
                    )
                )
                .scalars()
                .all()
            )
            for ad in ads:
                if not ad.external_id:
                    continue
                try:
                    rows = await adapter_for(account.platform, account.status == "practice").daily_metrics(
                        await context_for(session, ctx.services, account, purpose="sync ad metrics", campaign=ad),
                        ad.external_id,
                        max(today - timedelta(days=2), ad.starts_on or today),
                        today,
                    )
                except AuthExpired as exc:
                    account.status = "expired"
                    await notify(
                        session,
                        business_id,
                        kind="ads.account_expired",
                        level="critical",
                        title=f"Reconnect {account.name}",
                        body=str(exc),
                        link="/ads",
                    )
                    break
                except AdapterError as exc:
                    raise RetryLater(exc.retry_after or 300, str(exc)) from exc
                await _upsert_metrics(session, ad, rows)
                ad.last_synced_at = utcnow()
                synced += 1
            # Cap 1 (ours) is the last line of defence: the moment it's reached, everything stops.
            if account.monthly_cap_minor and await month_spend(session, account.id, today) >= account.monthly_cap_minor:
                for ad in ads:
                    if ad.status == "active":
                        await pause_in(session, ctx.services, ad, "Monthly ad cap reached", AGENT)
                        paused += 1
                if paused:
                    await notify(
                        session,
                        business_id,
                        kind="ads.cap_reached",
                        level="warning",
                        title=f"{account.name}: monthly cap reached",
                        body="All its campaigns are paused until next month or until you raise the cap.",
                        link="/ads",
                    )
                continue
            for ad in ads:
                if ad.status != "active":
                    continue
                history = (
                    (await session.execute(select(AdMetricDaily).where(AdMetricDaily.ad_campaign_id == ad.id)))
                    .scalars()
                    .all()
                )
                spend = sum(d.spend_minor for d in history)
                leads = sum(d.leads + d.purchases for d in history)
                target = await target_cpl(session, business, ad)
                reason = None
                if leads == 0 and spend >= ZERO_RESULT_BUDGET_DAYS * ad.daily_budget_minor > 0:
                    reason = f"Spent {spend / 100:,.2f} {account.currency} with no results yet."
                elif target and _cpl_breach(list(history), target):
                    reason = (
                        f"Cost per lead has been over {CPL_MULTIPLE:g}x the {target / 100:,.2f} target "
                        f"for {CPL_DAYS} days."
                    )
                if reason and not await _pause_pending(session, ad):
                    await propose_pause(session, business, ad, reason=reason, requested_by="pack:gtm.ads")
                    proposed += 1
    return {"synced": synced, "cap_paused": paused, "pause_proposed": proposed}


async def _pause_pending(session: AsyncSession, ad: AdCampaign) -> bool:
    """Don't stack pause proposals while one is already waiting or running."""
    from projectx.spine.decisions.models import Decision

    found = await session.scalar(
        select(Decision.id)
        .where(
            Decision.business_id == ad.business_id,
            Decision.action_type == "gtm.pause_ad",
            Decision.subject_id == ad.id,
            Decision.status.in_(("pending", "approved")),
        )
        .limit(1)
    )
    return found is not None


async def _upsert_metrics(session: AsyncSession, ad: AdCampaign, rows: list[DayMetrics]) -> None:
    for r in rows:
        values = {
            "spend_minor": r.spend_minor,
            "impressions": r.impressions,
            "clicks": r.clicks,
            "leads": r.leads,
            "purchases": r.purchases,
            "revenue_minor": r.revenue_minor,
        }
        await session.execute(
            insert(AdMetricDaily)
            .values(business_id=ad.business_id, ad_campaign_id=ad.id, day=r.day, **values)
            .on_conflict_do_update(index_elements=[AdMetricDaily.ad_campaign_id, AdMetricDaily.day], set_=values)
        )


# ---------------------------------------------------------------- views


def account_view(a: AdAccount, *, spent_this_month: int = 0) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "platform": a.platform,
        "external_id": a.external_id,
        "name": a.name,
        "currency": a.currency,
        "status": a.status,
        "monthly_cap_minor": a.monthly_cap_minor,
        "platform_spend_limit_minor": a.platform_spend_limit_minor,
        "spent_this_month_minor": spent_this_month,
        "cap_mirror_error": (a.settings or {}).get("cap_mirror_error"),
    }


def campaign_view(c: AdCampaign, metrics: list[AdMetricDaily]) -> dict[str, Any]:
    spend = sum(m.spend_minor for m in metrics)
    leads = sum(m.leads for m in metrics)
    return {
        "id": str(c.id),
        "ad_account_id": str(c.ad_account_id),
        "campaign_id": str(c.campaign_id) if c.campaign_id else None,
        "platform": c.platform,
        "external_id": c.external_id,
        "name": c.name,
        "objective": c.objective,
        "audience_kind": c.audience_kind,
        "daily_budget_minor": c.daily_budget_minor,
        "status": c.status,
        "special_category": c.special_category,
        "target_cpl_minor": c.target_cpl_minor,
        "paused_reason": c.paused_reason,
        "starts_on": c.starts_on.isoformat() if c.starts_on else None,
        "ends_on": c.ends_on.isoformat() if c.ends_on else None,
        "totals": {
            "spend_minor": spend,
            "impressions": sum(m.impressions for m in metrics),
            "clicks": sum(m.clicks for m in metrics),
            "leads": leads,
            "revenue_minor": sum(m.revenue_minor for m in metrics),
            "cpl_minor": round(spend / leads) if leads else None,
        },
        "series": [
            {"day": m.day.isoformat(), "spend_minor": m.spend_minor, "leads": m.leads, "clicks": m.clicks}
            for m in sorted(metrics, key=lambda m: m.day)
        ],
    }


# ---------------------------------------------------------------- Scale plan: share of ad spend


@job("gtm.ads_share", max_attempts=6, timeout_s=120, priority=60)
async def report_ad_share(ctx: JobContext) -> dict[str, Any]:
    """On the 1st: report last month's management fee (a share of ad spend) to billing.

    Dodo meters charge in USD. Spend in another currency is recorded and flagged for a manual invoice
    rather than converted at a rate nobody agreed to.
    """
    from projectx.spine.billing import service as billing
    from projectx.spine.billing.plans import plan_for

    business_id = ctx.require_business()
    period = str(ctx.payload["period"])  # YYYY-MM
    year, month = (int(x) for x in period.split("-"))
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    async with ctx.services.db.tenant(business_id) as session:
        business = await session.get(Business, business_id)
        assert business is not None
        bps = plan_for(business.plan).ad_spend_share_bps
        if not bps:
            return {"skipped": "plan has no ad-spend share"}
        rows = (
            await session.execute(
                select(AdAccount.currency, func.coalesce(func.sum(AdMetricDaily.spend_minor), 0))
                .join(AdCampaign, AdCampaign.ad_account_id == AdAccount.id)
                .join(AdMetricDaily, AdMetricDaily.ad_campaign_id == AdCampaign.id)
                .where(
                    AdAccount.business_id == business_id,
                    AdAccount.status != "practice",
                    AdMetricDaily.day >= start,
                    AdMetricDaily.day <= end,
                )
                .group_by(AdAccount.currency)
            )
        ).all()
        sub = await billing.subscription_of(session, business_id)
        reported = []
        for currency, spend in rows:
            fee = int(spend) * bps // 10_000
            if fee <= 0:
                continue
            charged = False
            if currency == "USD" and sub.provider_customer_id and ctx.services.settings.dodo_api_key:
                await billing.client(ctx.services).ingest_usage(
                    event_id=f"adshare:{business_id}:{period}:{currency}",  # idempotent at the provider too
                    customer_id=sub.provider_customer_id,
                    event_name=ctx.services.settings.dodo_ad_share_event,
                    metadata={"fee_cents": fee, "ad_spend_cents": int(spend), "period": period},
                )
                charged = True
            else:
                await notify(
                    session,
                    business_id,
                    kind="billing.ad_share_manual",
                    level="info",
                    title=f"Ad management fee for {period}: {fee / 100:,.2f} {currency}",
                    body="We'll invoice this separately because it isn't in US dollars.",
                    link="/settings?tab=plan",
                )
            await append(
                session,
                business_id=business_id,
                type="spine.billing.usage",
                actor=AGENT,
                data={
                    "metric": "ad_spend_share",
                    "amount_minor": fee,
                    "currency": currency,
                    "period": period,
                    "charged": charged,
                },
                idempotency_key=f"adshare:{period}:{currency}",
            )
            reported.append({"currency": currency, "fee_minor": fee, "charged": charged})
    return {"period": period, "reported": reported}
