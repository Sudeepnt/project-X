"""The model gateway: cheapest rung first, verified answers, cost log, cache, daily cap."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from projectx.services import Services
from projectx.settings import Settings
from projectx.spine.gateway.contracts import (
    ChatMessage,
    ComputeCapReached,
    ModelRequest,
    ProviderRefusal,
    ProviderResponse,
)
from projectx.spine.gateway.gateway import ModelGateway
from projectx.spine.gateway.models import ModelCall


class Headline(BaseModel):
    headline: str
    score: float


class FakeProvider:
    def __init__(self, name: str, replies: dict[str, str | Exception]) -> None:
        self.name = name
        self.replies = replies
        self.calls: list[str] = []

    async def complete(self, *, model: str, req: ModelRequest) -> ProviderResponse:
        self.calls.append(model)
        reply = self.replies[model]
        if isinstance(reply, Exception):
            raise reply
        return ProviderResponse(text=reply, model=model, input_tokens=1000, output_tokens=200)


def _gateway(settings: Settings, services: Services, provider: FakeProvider, cap: int = 10_000_000) -> ModelGateway:
    s = settings.model_copy(
        update={"llm_small": "fake:small", "llm_mid": "fake:mid", "llm_frontier": "fake:claude-opus-5-5"}
    )

    async def cap_resolver(_: uuid.UUID) -> int:
        return cap

    return ModelGateway(s, services.db, providers={"fake": provider}, cap_resolver=cap_resolver)  # type: ignore[dict-item]


def _req(**kw: Any) -> ModelRequest:
    return ModelRequest(
        task="test.headline",
        system="You write headlines.",
        messages=[ChatMessage("user", f"Write one {uuid.uuid4()}")],
        output_schema=Headline,
        **kw,
    )


async def test_offline_provider_returns_schema_valid_drafts(services: Services, business: dict[str, Any]) -> None:
    result = await services.gateway.run(uuid.UUID(business["id"]), _req())
    assert isinstance(result.parsed, Headline)
    assert result.provider == "offline"


async def test_climbs_only_when_the_check_fails(
    settings: Settings, services: Services, business: dict[str, Any]
) -> None:
    provider = FakeProvider(
        "fake",
        {
            "small": "not json",
            "mid": ProviderRefusal("declined"),
            "claude-opus-5-5": '{"headline": "Launch day", "score": 0.9}',
        },
    )
    gw = _gateway(settings, services, provider)
    bid = uuid.UUID(business["id"])
    result = await gw.run(bid, _req())
    assert provider.calls == ["small", "mid", "claude-opus-5-5"]
    assert result.rung == "frontier"
    assert result.parsed_as(Headline).headline == "Launch day"
    # Opus 5.5 pricing: 1000 in * $4/M + 200 out * $20/M = 8000 micro-USD.
    assert result.cost_micro_usd == 8000
    async with services.db.tenant(bid) as s:
        statuses = (
            (
                await s.execute(
                    select(ModelCall.status)
                    .where(ModelCall.business_id == bid, ModelCall.task == "test.headline")
                    .order_by(ModelCall.created_at)
                )
            )
            .scalars()
            .all()
        )
    assert statuses[-3:] == ["invalid", "refused", "ok"]


async def test_verifier_failure_escalates(settings: Settings, services: Services, business: dict[str, Any]) -> None:
    provider = FakeProvider(
        "fake",
        {
            "small": '{"headline": "meh", "score": 0.1}',
            "mid": '{"headline": "Great", "score": 0.8}',
            "claude-opus-5-5": '{"headline": "x", "score": 1}',
        },
    )
    gw = _gateway(settings, services, provider)

    def verifier(result: Any) -> str | None:
        return "score too low" if result.parsed.score < 0.5 else None

    result = await gw.run(uuid.UUID(business["id"]), _req(verifier=verifier))
    assert result.rung == "mid"


async def test_cache_never_pays_twice(settings: Settings, services: Services, business: dict[str, Any]) -> None:
    provider = FakeProvider("fake", {"small": '{"headline": "Once", "score": 0.7}'})
    gw = _gateway(settings, services, provider)
    req = _req(max_rung="small")
    first = await gw.run(uuid.UUID(business["id"]), req)
    second = await gw.run(uuid.UUID(business["id"]), req)
    assert provider.calls == ["small"]
    assert second.cached and second.cost_micro_usd == 0
    assert second.parsed_as(Headline).headline == first.parsed_as(Headline).headline


async def test_daily_cap_stops_spending(settings: Settings, services: Services, business: dict[str, Any]) -> None:
    provider = FakeProvider("fake", {"small": '{"headline": "x", "score": 0.7}'})
    gw = _gateway(settings, services, provider, cap=1)
    with pytest.raises(ComputeCapReached):
        await gw.run(
            uuid.UUID(business["id"]),
            ModelRequest(
                task="test.capped", system="s", messages=[ChatMessage("user", "hi")], max_tokens=1000, rung="frontier"
            ),
        )
    assert provider.calls == []


async def test_claude_only_for_hard_jobs(settings: Settings, services: Services, business: dict[str, Any]) -> None:
    s = settings.model_copy(
        update={
            "llm_small": "openai:qwen-small",
            "llm_mid": "anthropic:claude-sonnet-5-5",  # misconfigured on purpose: must never be used for normal work
            "llm_frontier": "openai:open-large",
            "llm_hard": "anthropic:claude-opus-5-5",
        }
    )
    open_models = FakeProvider("openai", {"qwen-small": "plain", "open-large": "plain"})
    claude = FakeProvider("anthropic", {"claude-opus-5-5": "hard answer", "claude-sonnet-5-5": "nope"})
    gw = ModelGateway(s, services.db, providers={"openai": open_models, "anthropic": claude})  # type: ignore[dict-item]
    chain = gw._chain(ModelRequest(task="t", system="", messages=[], rung="small", max_rung="frontier"))
    assert [(p, m) for _, p, m in chain] == [("openai", "qwen-small"), ("openai", "open-large")]
    hard = gw._chain(ModelRequest(task="t", system="", messages=[], hard=True))
    assert next((p, m) for _, p, m in hard) == ("anthropic", "claude-opus-5-5")
    assert ("anthropic", "claude-sonnet-5-5") in [(p, m) for _, p, m in hard]  # allowed only because the job is hard
    bid = uuid.UUID(business["id"])
    out = await gw.run(bid, ModelRequest(task="code.fix", system="", messages=[ChatMessage("user", "x")], hard=True))
    assert out.text == "hard answer" and claude.calls == ["claude-opus-5-5"]
    assert gw.describe()["mid"]["available"] is False and gw.describe()["hard"]["available"] is True
