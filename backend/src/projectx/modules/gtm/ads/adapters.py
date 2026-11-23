"""Ad platform adapters: a deterministic simulator (practice mode), Meta Marketing API and Google Ads REST.

Every adapter works in the account's own currency and minor units. Meta and Google bill the customer
directly; we only steer budgets inside the caps the founder set.

Meta: we create the whole chain (campaign → ad set → creative → ad) from a post that already worked.
Google: Search/PMax campaigns need keywords and asset groups that are best made in Google Ads itself,
so we *link* an existing campaign and manage its budget, status and numbers.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

import httpx

from projectx.modules.gtm.channels.adapters.base import AdapterError, AuthExpired, PlatformRejected

META_GRAPH_VERSION = "v24.0"
GOOGLE_ADS_VERSION = "v21"

# Special ad categories as Meta names them. Politics is never automated (see service.py).
META_SPECIAL_CATEGORIES = {"housing": "HOUSING", "employment": "EMPLOYMENT", "credit": "FINANCIAL_PRODUCTS_SERVICES"}


@dataclass
class AdsContext:
    http: httpx.AsyncClient
    credentials: dict[str, Any]
    account_external_id: str
    settings: dict[str, Any]
    currency: str


@dataclass
class AdSpec:
    name: str
    objective: str  # leads | traffic | sales
    daily_budget_minor: int
    audience_kind: str  # retargeting | lookalike | cold
    special_category: str | None
    country: str | None
    landing_url: str | None
    message: str = ""
    headline: str = ""
    image_url: str | None = None
    external_campaign_id: str | None = None  # Google: the campaign to link
    idempotency_key: str = ""


@dataclass
class CreatedAd:
    external_id: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class DayMetrics:
    day: date
    spend_minor: int
    impressions: int
    clicks: int
    leads: int
    purchases: int = 0
    revenue_minor: int = 0


class AdsAdapter(Protocol):
    platform: str

    async def create(self, ctx: AdsContext, spec: AdSpec) -> CreatedAd: ...

    async def set_daily_budget(self, ctx: AdsContext, external_id: str, daily_budget_minor: int) -> None: ...

    async def set_status(self, ctx: AdsContext, external_id: str, active: bool) -> None: ...

    async def daily_metrics(self, ctx: AdsContext, external_id: str, since: date, until: date) -> list[DayMetrics]: ...

    async def set_spend_limit(self, ctx: AdsContext, limit_minor: int | None) -> bool:
        """Mirror our monthly cap to the platform. Returns False where the platform has no such limit."""
        ...


def _raise_for(response: httpx.Response, platform: str) -> None:
    if response.status_code < 400:
        return
    try:
        body = response.json()
    except ValueError:
        body = {}
    err = body.get("error", body) if isinstance(body, dict) else {}
    message = str(err.get("message") or err.get("error_user_msg") or response.text[:300])
    if response.status_code in (401, 403) or err.get("code") in (190, 102):
        raise AuthExpired(f"{platform}: {message}")
    if response.status_code == 429 or err.get("code") in (4, 17, 32, 613):
        retry = response.headers.get("retry-after")
        raise AdapterError(f"{platform} rate limit: {message}", retry_after=float(retry) if retry else 60.0)
    if response.status_code >= 500:
        raise AdapterError(f"{platform} is having trouble: {message}")
    raise PlatformRejected(f"{platform} refused: {message}")


# ---------------------------------------------------------------- practice


class SimulatedAds:
    """Practice mode: believable, deterministic numbers. Nothing leaves the app."""

    platform = "simulator"

    async def create(self, ctx: AdsContext, spec: AdSpec) -> CreatedAd:
        digest = hashlib.sha256(f"{ctx.account_external_id}:{spec.idempotency_key or spec.name}".encode()).hexdigest()
        return CreatedAd(external_id=f"sim-{digest[:16]}", extra={"practice": True})

    async def set_daily_budget(self, ctx: AdsContext, external_id: str, daily_budget_minor: int) -> None:
        return None

    async def set_status(self, ctx: AdsContext, external_id: str, active: bool) -> None:
        return None

    async def daily_metrics(self, ctx: AdsContext, external_id: str, since: date, until: date) -> list[DayMetrics]:
        budget = int(ctx.settings.get("daily_budget_minor", 0))
        quality = float(ctx.settings.get("sim_quality", 1.0))  # below 1 makes a campaign convert worse
        out = []
        day = since
        while day <= until:
            rng = random.Random(f"{external_id}:{day.isoformat()}")  # noqa: S311 - simulation
            spend = int(budget * rng.uniform(0.85, 1.0))
            impressions = int(spend / 100 * rng.uniform(80, 140)) if spend else 0
            clicks = int(impressions * rng.uniform(0.008, 0.02))
            leads = int(clicks * rng.uniform(0.08, 0.2) * quality)
            out.append(DayMetrics(day=day, spend_minor=spend, impressions=impressions, clicks=clicks, leads=leads))
            day = date.fromordinal(day.toordinal() + 1)
        return out

    async def set_spend_limit(self, ctx: AdsContext, limit_minor: int | None) -> bool:
        return True


# ---------------------------------------------------------------- Meta


class MetaAds:
    platform = "meta"

    def _base(self, ctx: AdsContext) -> str:
        return f"https://graph.facebook.com/{ctx.settings.get('graph_version', META_GRAPH_VERSION)}"

    def _act(self, ctx: AdsContext) -> str:
        acct = ctx.account_external_id
        return acct if acct.startswith("act_") else f"act_{acct}"

    async def _post(self, ctx: AdsContext, path: str, data: dict[str, Any]) -> dict[str, Any]:
        response = await ctx.http.post(
            f"{self._base(ctx)}/{path}", data={**data, "access_token": ctx.credentials["access_token"]}
        )
        _raise_for(response, "Meta")
        result: dict[str, Any] = response.json()
        return result

    async def create(self, ctx: AdsContext, spec: AdSpec) -> CreatedAd:
        page_id = ctx.settings.get("page_id")
        if not page_id or not spec.landing_url:
            raise PlatformRejected("Meta ads need a Facebook Page and a landing page link")
        import orjson

        objective = {"leads": "OUTCOME_LEADS", "traffic": "OUTCOME_TRAFFIC", "sales": "OUTCOME_SALES"}[spec.objective]
        special = [META_SPECIAL_CATEGORIES[spec.special_category]] if spec.special_category else []
        campaign = await self._post(
            ctx,
            f"{self._act(ctx)}/campaigns",
            {
                "name": spec.name[:200],
                "objective": objective,
                "status": "PAUSED",  # flipped to ACTIVE only once the whole chain exists
                "special_ad_categories": orjson.dumps(special).decode(),
            },
        )
        targeting: dict[str, Any] = {"geo_locations": {"countries": [spec.country or "US"]}}
        if spec.audience_kind == "retargeting" and ctx.settings.get("retargeting_audience_id"):
            targeting["custom_audiences"] = [{"id": ctx.settings["retargeting_audience_id"]}]
        elif spec.audience_kind == "lookalike" and ctx.settings.get("lookalike_audience_id"):
            targeting["custom_audiences"] = [{"id": ctx.settings["lookalike_audience_id"]}]
        adset = await self._post(
            ctx,
            f"{self._act(ctx)}/adsets",
            {
                "name": f"{spec.name[:180]} · audience",
                "campaign_id": campaign["id"],
                "daily_budget": str(spec.daily_budget_minor),
                "billing_event": "IMPRESSIONS",
                "optimization_goal": "LINK_CLICKS",
                "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                "targeting": orjson.dumps(targeting).decode(),
                "status": "ACTIVE",
            },
        )
        link_data: dict[str, Any] = {"message": spec.message[:2000], "link": spec.landing_url, "name": spec.headline}
        if spec.image_url:
            link_data["picture"] = spec.image_url
        creative = await self._post(
            ctx,
            f"{self._act(ctx)}/adcreatives",
            {
                "name": f"{spec.name[:180]} · creative",
                "object_story_spec": orjson.dumps({"page_id": page_id, "link_data": link_data}).decode(),
            },
        )
        await self._post(
            ctx,
            f"{self._act(ctx)}/ads",
            {
                "name": f"{spec.name[:180]} · ad",
                "adset_id": adset["id"],
                "creative": orjson.dumps({"creative_id": creative["id"]}).decode(),
                "status": "ACTIVE",
            },
        )
        await self._post(ctx, campaign["id"], {"status": "ACTIVE"})
        return CreatedAd(external_id=campaign["id"], extra={"adset_id": adset["id"], "creative_id": creative["id"]})

    async def set_daily_budget(self, ctx: AdsContext, external_id: str, daily_budget_minor: int) -> None:
        response = await ctx.http.get(
            f"{self._base(ctx)}/{external_id}/adsets",
            params={"fields": "id", "access_token": ctx.credentials["access_token"]},
        )
        _raise_for(response, "Meta")
        for adset in response.json().get("data", []):
            await self._post(ctx, adset["id"], {"daily_budget": str(daily_budget_minor)})

    async def set_status(self, ctx: AdsContext, external_id: str, active: bool) -> None:
        await self._post(ctx, external_id, {"status": "ACTIVE" if active else "PAUSED"})

    async def daily_metrics(self, ctx: AdsContext, external_id: str, since: date, until: date) -> list[DayMetrics]:
        import orjson

        response = await ctx.http.get(
            f"{self._base(ctx)}/{external_id}/insights",
            params={
                "fields": "spend,impressions,clicks,actions",
                "time_increment": "1",
                "time_range": orjson.dumps({"since": since.isoformat(), "until": until.isoformat()}).decode(),
                "access_token": ctx.credentials["access_token"],
            },
        )
        _raise_for(response, "Meta")
        out = []
        for row in response.json().get("data", []):
            actions = {a["action_type"]: float(a["value"]) for a in row.get("actions", [])}
            leads = actions.get("lead", 0) + actions.get("onsite_conversion.lead_grouped", 0)
            purchases = actions.get("purchase", 0) + actions.get("offsite_conversion.fb_pixel_purchase", 0)
            out.append(
                DayMetrics(
                    day=date.fromisoformat(row["date_start"]),
                    spend_minor=round(float(row.get("spend", 0)) * 100),
                    impressions=int(row.get("impressions", 0)),
                    clicks=int(row.get("clicks", 0)),
                    leads=int(leads),
                    purchases=int(purchases),
                )
            )
        return out

    async def set_spend_limit(self, ctx: AdsContext, limit_minor: int | None) -> bool:
        # Meta's account spend cap is lifetime and in major units; 0 removes it.
        await self._post(ctx, self._act(ctx), {"spend_cap": str((limit_minor or 0) // 100)})
        return True


# ---------------------------------------------------------------- Google


class GoogleAds:
    platform = "google"

    def _headers(self, ctx: AdsContext) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {ctx.credentials['access_token']}",
            "developer-token": ctx.credentials["developer_token"],
            "Content-Type": "application/json",
        }
        if ctx.credentials.get("login_customer_id"):
            headers["login-customer-id"] = str(ctx.credentials["login_customer_id"]).replace("-", "")
        return headers

    def _base(self, ctx: AdsContext) -> str:
        customer = ctx.account_external_id.replace("-", "")
        version = ctx.settings.get("api_version", GOOGLE_ADS_VERSION)
        return f"https://googleads.googleapis.com/{version}/customers/{customer}"

    async def _search(self, ctx: AdsContext, query: str) -> list[dict[str, Any]]:
        response = await ctx.http.post(
            f"{self._base(ctx)}/googleAds:searchStream", json={"query": query}, headers=self._headers(ctx)
        )
        _raise_for(response, "Google Ads")
        rows: list[dict[str, Any]] = []
        for chunk in response.json():
            rows.extend(chunk.get("results", []))
        return rows

    async def create(self, ctx: AdsContext, spec: AdSpec) -> CreatedAd:
        if not spec.external_campaign_id or not spec.external_campaign_id.isdigit():
            raise PlatformRejected("Pick an existing Google Ads campaign to link; create it in Google Ads first")
        rows = await self._search(
            ctx,
            # GAQL takes no bind parameters; every interpolated value here is int() or an ISO date.
            f"SELECT campaign.id, campaign.campaign_budget FROM campaign WHERE campaign.id = {int(spec.external_campaign_id)}",  # noqa: S608, E501
        )
        if not rows:
            raise PlatformRejected("That campaign isn't in this Google Ads account")
        await self.set_daily_budget(ctx, spec.external_campaign_id, spec.daily_budget_minor)
        await self.set_status(ctx, spec.external_campaign_id, True)
        return CreatedAd(external_id=spec.external_campaign_id, extra={"budget": rows[0]["campaign"]["campaignBudget"]})

    async def set_daily_budget(self, ctx: AdsContext, external_id: str, daily_budget_minor: int) -> None:
        rows = await self._search(
            ctx,
            f"SELECT campaign.campaign_budget FROM campaign WHERE campaign.id = {int(external_id)}",  # noqa: S608
        )
        if not rows:
            raise PlatformRejected("Campaign not found in Google Ads")
        budget = rows[0]["campaign"]["campaignBudget"]
        response = await ctx.http.post(
            f"{self._base(ctx)}/campaignBudgets:mutate",
            json={
                "operations": [
                    {
                        "update": {"resourceName": budget, "amountMicros": str(daily_budget_minor * 10_000)},
                        "updateMask": "amount_micros",
                    }
                ]
            },
            headers=self._headers(ctx),
        )
        _raise_for(response, "Google Ads")

    async def set_status(self, ctx: AdsContext, external_id: str, active: bool) -> None:
        customer = ctx.account_external_id.replace("-", "")
        response = await ctx.http.post(
            f"{self._base(ctx)}/campaigns:mutate",
            json={
                "operations": [
                    {
                        "update": {
                            "resourceName": f"customers/{customer}/campaigns/{int(external_id)}",
                            "status": "ENABLED" if active else "PAUSED",
                        },
                        "updateMask": "status",
                    }
                ]
            },
            headers=self._headers(ctx),
        )
        _raise_for(response, "Google Ads")

    async def daily_metrics(self, ctx: AdsContext, external_id: str, since: date, until: date) -> list[DayMetrics]:
        rows = await self._search(
            ctx,
            "SELECT segments.date, metrics.cost_micros, metrics.impressions, metrics.clicks, metrics.conversions, "  # noqa: S608
            f"metrics.conversions_value FROM campaign WHERE campaign.id = {int(external_id)} "
            f"AND segments.date BETWEEN '{since.isoformat()}' AND '{until.isoformat()}'",
        )
        out = []
        for row in rows:
            m = row.get("metrics", {})
            out.append(
                DayMetrics(
                    day=date.fromisoformat(row["segments"]["date"]),
                    spend_minor=int(m.get("costMicros", 0)) // 10_000,
                    impressions=int(m.get("impressions", 0)),
                    clicks=int(m.get("clicks", 0)),
                    leads=int(float(m.get("conversions", 0))),
                    revenue_minor=round(float(m.get("conversionsValue", 0)) * 100),
                )
            )
        return out

    async def set_spend_limit(self, ctx: AdsContext, limit_minor: int | None) -> bool:
        return False  # account budgets need invoice billing; our own cap check pauses campaigns instead


_SIMULATOR = SimulatedAds()


def adapter_for(platform: str, practice: bool) -> AdsAdapter:
    if practice:
        return _SIMULATOR
    if platform == "meta":
        return MetaAds()
    if platform == "google":
        return GoogleAds()
    raise PlatformRejected(f"Unknown ad platform {platform!r}")


REQUIRED_CREDENTIALS = {"meta": ("access_token",), "google": ("access_token", "developer_token")}
