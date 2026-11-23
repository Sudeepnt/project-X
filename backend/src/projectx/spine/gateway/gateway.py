"""The model gateway: one door to every model (blueprint §8, Figure 4).

For each request it (1) checks the business's daily compute cap, (2) serves an identical earlier
answer from cache, (3) starts on the lowest configured rung, validates the answer against the
output schema and the request's verifier, and climbs one rung only when that check fails, and
(4) logs every attempt with tokens and cost in its own transaction, so the spending report is
complete even when the caller's work rolls back.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import orjson
import structlog
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from projectx.db.database import Database
from projectx.kernel.time import utcnow
from projectx.settings import Settings
from projectx.spine.gateway.contracts import (
    RUNG_ORDER,
    ComputeCapReached,
    ModelRequest,
    ModelResult,
    ModelsExhausted,
    ModelsRateLimited,
    ProviderError,
    ProviderRateLimited,
    Rung,
)
from projectx.spine.gateway.models import ModelCache, ModelCall
from projectx.spine.gateway.pricing import cost_micro_usd, worst_case_micro_usd
from projectx.spine.gateway.providers import (
    AnthropicProvider,
    OfflineProvider,
    OpenAICompatibleProvider,
    Provider,
)
from projectx.spine.gateway.schema import strict_json_schema

log = structlog.get_logger("gateway")

CapResolver = Callable[[uuid.UUID], Awaitable[int]]


def parse_rung(spec: str) -> tuple[str, str]:
    provider, _, model = spec.partition(":")
    if not provider or not model:
        raise ValueError(f"model rung must look like 'provider:model', got {spec!r}")
    return provider, model


class ModelGateway:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        *,
        cap_resolver: CapResolver | None = None,
        providers: dict[str, Provider] | None = None,
    ) -> None:
        self._settings = settings
        self._db = db
        self._rungs: dict[Rung, tuple[str, str]] = {
            "small": parse_rung(settings.llm_small),
            "mid": parse_rung(settings.llm_mid),
            "frontier": parse_rung(settings.llm_frontier),
        }
        self._providers: dict[str, Provider] = providers if providers is not None else self._build_providers(settings)
        self._cap_resolver = cap_resolver
        self._cache_ttl = timedelta(hours=settings.llm_cache_ttl_hours)

    @staticmethod
    def _build_providers(settings: Settings) -> dict[str, Provider]:
        providers: dict[str, Provider] = {"offline": OfflineProvider()}
        if settings.anthropic_api_key:
            providers["anthropic"] = AnthropicProvider(settings.anthropic_api_key.get_secret_value())
        if settings.openai_compat_base_url:
            key = settings.openai_compat_api_key.get_secret_value() if settings.openai_compat_api_key else None
            providers["openai"] = OpenAICompatibleProvider(settings.openai_compat_base_url, key)
        return providers

    def describe(self) -> dict[str, Any]:
        """What each rung points at, for the settings screen. No secrets."""
        return {
            rung: {"provider": provider, "model": model, "available": provider in self._providers}
            for rung, (provider, model) in self._rungs.items()
        }

    def set_cap_resolver(self, resolver: CapResolver) -> None:
        self._cap_resolver = resolver

    async def spent_today_micro_usd(self, business_id: uuid.UUID) -> int:
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        async with self._db.tenant(business_id) as session:
            total = await session.scalar(
                select(func.coalesce(func.sum(ModelCall.cost_micro_usd), 0)).where(
                    ModelCall.business_id == business_id, ModelCall.created_at >= start
                )
            )
        return int(total or 0)

    async def _daily_cap(self, business_id: uuid.UUID) -> int:
        if self._cap_resolver is not None:
            return await self._cap_resolver(business_id)
        return int(self._settings.llm_default_daily_cap_usd * 1_000_000)

    def _request_hash(self, req: ModelRequest, chain: list[tuple[Rung, str, str]]) -> str:
        material = orjson.dumps(
            {
                "task": req.task,
                "system": req.system,
                "messages": [[m.role, m.content] for m in req.messages],
                "schema": strict_json_schema(req.output_schema) if req.output_schema else None,
                "effort": req.effort,
                "max_tokens": req.max_tokens,
                "chain": [[r, p, m] for r, p, m in chain],
            },
            option=orjson.OPT_SORT_KEYS,
        )
        return hashlib.sha256(material).hexdigest()

    def _chain(self, req: ModelRequest) -> list[tuple[Rung, str, str]]:
        start, stop = RUNG_ORDER.index(req.rung), RUNG_ORDER.index(req.max_rung)
        chain: list[tuple[Rung, str, str]] = []
        seen: set[tuple[str, str]] = set()
        for rung in RUNG_ORDER[start : stop + 1]:
            provider, model = self._rungs[rung]
            if provider in self._providers and (provider, model) not in seen:
                chain.append((rung, provider, model))
                seen.add((provider, model))
        if not chain:
            chain.append((req.rung, "offline", "draft-fallback"))
        return chain

    async def run(
        self, business_id: uuid.UUID, req: ModelRequest, *, correlation_id: uuid.UUID | None = None
    ) -> ModelResult:
        chain = self._chain(req)
        request_hash = self._request_hash(req, chain)

        if req.cache:
            cached = await self._cache_get(business_id, request_hash)
            if cached is not None:
                parsed = (
                    req.output_schema.model_validate(cached["parsed"])
                    if req.output_schema and cached.get("parsed") is not None
                    else None
                )
                call_id = await self._log(
                    business_id,
                    req,
                    rung=chain[0][0],
                    provider="cache",
                    model=cached["model"],
                    status="cached",
                    request_hash=request_hash,
                    cached=True,
                    response={"text": cached["text"]},
                    correlation_id=correlation_id,
                )
                return ModelResult(cached["text"], parsed, "cache", cached["model"], chain[0][0], 0, True, 0, [call_id])

        cap = await self._daily_cap(business_id)
        spent = await self.spent_today_micro_usd(business_id)
        prompt_chars = len(req.system) + sum(len(m.content) for m in req.messages)
        worst = worst_case_micro_usd(chain[0][2], prompt_chars=prompt_chars, max_tokens=req.max_tokens)
        if spent + worst > cap:
            raise ComputeCapReached(spent, cap)

        call_ids: list[uuid.UUID] = []
        rate_limited_for: float | None = None
        problems: list[str] = []
        for attempt, (rung, provider_name, model) in enumerate(chain, start=1):
            provider = self._providers[provider_name]
            started = time.perf_counter()
            try:
                resp = await provider.complete(model=model, req=req)
            except ProviderRateLimited as exc:
                rate_limited_for = max(rate_limited_for or 0.0, exc.retry_after)
                call_ids.append(
                    await self._log(
                        business_id,
                        req,
                        rung=rung,
                        provider=provider_name,
                        model=model,
                        status=exc.status,
                        request_hash=request_hash,
                        error=str(exc),
                        latency_ms=_ms(started),
                        correlation_id=correlation_id,
                    )
                )
                continue
            except ProviderError as exc:
                problems.append(f"{rung}:{exc}")
                call_ids.append(
                    await self._log(
                        business_id,
                        req,
                        rung=rung,
                        provider=provider_name,
                        model=model,
                        status=exc.status,
                        request_hash=request_hash,
                        error=str(exc),
                        latency_ms=_ms(started),
                        correlation_id=correlation_id,
                    )
                )
                continue

            cost = cost_micro_usd(
                resp.model if resp.model.startswith("claude") else model,
                input_tokens=resp.input_tokens,
                output_tokens=resp.output_tokens,
                cache_read_tokens=resp.cache_read_tokens,
                cache_write_tokens=resp.cache_write_tokens,
            )
            usage = {
                "input_tokens": resp.input_tokens,
                "output_tokens": resp.output_tokens,
                "cache_read_tokens": resp.cache_read_tokens,
                "cache_write_tokens": resp.cache_write_tokens,
            }
            parsed = None
            status, error = "ok", None
            if req.output_schema is not None:
                try:
                    parsed = req.output_schema.model_validate_json(resp.text)
                except ValidationError as exc:
                    status, error = "invalid", f"schema validation failed: {exc.error_count()} errors"
            result = ModelResult(resp.text, parsed, provider_name, resp.model, rung, cost, False, attempt, call_ids)
            if status == "ok" and req.verifier is not None:
                problem = req.verifier(result)
                if problem:
                    status, error = "verifier_failed", problem
            call_id = await self._log(
                business_id,
                req,
                rung=rung,
                provider=provider_name,
                model=resp.model,
                status=status,
                request_hash=request_hash,
                cost=cost,
                usage=usage,
                response={"text": resp.text[:20000]},
                error=error,
                latency_ms=_ms(started),
                correlation_id=correlation_id,
            )
            call_ids.append(call_id)
            if status != "ok":
                problems.append(f"{rung}:{error}")
                continue
            result.call_ids = call_ids
            if req.cache:
                await self._cache_put(business_id, request_hash, result)
            return result

        if rate_limited_for is not None and not problems:
            raise ModelsRateLimited(rate_limited_for)
        raise ModelsExhausted("; ".join(problems) or "no model produced an acceptable answer")

    async def _log(
        self,
        business_id: uuid.UUID,
        req: ModelRequest,
        *,
        rung: Rung,
        provider: str,
        model: str,
        status: str,
        request_hash: str,
        cost: int = 0,
        usage: dict[str, int] | None = None,
        response: dict[str, Any] | None = None,
        error: str | None = None,
        latency_ms: int = 0,
        cached: bool = False,
        correlation_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        prompt = None
        if self._settings.llm_log_prompts:
            prompt = {
                "system": req.system[:20000],
                "messages": [[m.role, m.content[:20000]] for m in req.messages],
                "context": req.context,
            }
        call = ModelCall(
            business_id=business_id,
            task=req.task,
            rung=rung,
            provider=provider,
            model=model,
            status=status,
            cost_micro_usd=cost,
            latency_ms=latency_ms,
            cached=cached,
            request_hash=request_hash,
            prompt=prompt,
            response=response,
            error=error,
            correlation_id=correlation_id,
            **(usage or {}),
        )
        try:
            async with self._db.tenant(business_id) as session:
                session.add(call)
        except Exception:  # cost logging must never break the work itself
            log.exception("gateway.log_failed", task=req.task)
        return call.id

    async def _cache_get(self, business_id: uuid.UUID, request_hash: str) -> dict[str, Any] | None:
        async with self._db.tenant(business_id) as session:
            row = await session.scalar(
                select(ModelCache).where(
                    ModelCache.business_id == business_id,
                    ModelCache.request_hash == request_hash,
                    ModelCache.expires_at > func.now(),
                )
            )
            return dict(row.response) if row is not None else None

    async def _cache_put(self, business_id: uuid.UUID, request_hash: str, result: ModelResult) -> None:
        payload = {
            "text": result.text,
            "model": result.model,
            "parsed": result.parsed.model_dump(mode="json") if result.parsed is not None else None,
        }
        try:
            async with self._db.tenant(business_id) as session:
                stmt = insert(ModelCache).values(
                    business_id=business_id,
                    request_hash=request_hash,
                    response=payload,
                    model=result.model,
                    expires_at=utcnow() + self._cache_ttl,
                )
                await session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[ModelCache.business_id, ModelCache.request_hash],
                        set_={"response": payload, "model": result.model, "expires_at": utcnow() + self._cache_ttl},
                    )
                )
        except Exception:
            log.exception("gateway.cache_put_failed")


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
