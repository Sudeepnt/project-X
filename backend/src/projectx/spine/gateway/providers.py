"""Model providers behind the gateway.

* ``anthropic`` — Claude through the official SDK. Opus/Sonnet calls opt into server-side refusal
  fallbacks so a declined request is re-run on a fallback model inside the same call.
* ``openai`` — any OpenAI-compatible endpoint (vLLM, Ollama, LiteLLM proxy, hosted open models):
  the blueprint's rented open models and, later, our own fine-tuned small models.
* ``offline`` — deterministic drafts with no network. Development, tests, demos and practice mode.
"""

from __future__ import annotations

import re
from typing import Any, Protocol

import anthropic
import httpx
import orjson

from projectx.spine.gateway.contracts import (
    ModelRequest,
    ProviderError,
    ProviderRateLimited,
    ProviderRefusal,
    ProviderResponse,
    ProviderTruncated,
    ProviderUnavailable,
)
from projectx.spine.gateway.offline import generate_offline
from projectx.spine.gateway.schema import strict_json_schema

# Models that accept the server-side refusal fallback parameter (Claude API, beta header).
_SERVER_FALLBACK_MODELS = re.compile(r"^claude-(opus-5-5|opus-5|sonnet-5-5|fable-5-[12])$")


class Provider(Protocol):
    name: str

    async def complete(self, *, model: str, req: ModelRequest) -> ProviderResponse: ...


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, *, timeout_s: float = 120.0) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=2, timeout=timeout_s)

    async def complete(self, *, model: str, req: ModelRequest) -> ProviderResponse:
        output_config: dict[str, Any] = {"effort": req.effort}
        if req.output_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": strict_json_schema(req.output_schema)}
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": req.max_tokens,
            "system": req.system,
            "messages": [{"role": m.role, "content": m.content} for m in req.messages],
            "output_config": output_config,
            "cache_control": {"type": "ephemeral"},
        }
        try:
            if _SERVER_FALLBACK_MODELS.match(model):
                response: Any = await self._client.beta.messages.create(
                    **params, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
                )
            else:
                response = await self._client.messages.create(**params)
        except anthropic.RateLimitError as exc:
            retry_after = float(exc.response.headers.get("retry-after", "30"))
            raise ProviderRateLimited(str(exc), retry_after) from exc
        except anthropic.BadRequestError as exc:
            raise ProviderError(f"bad request: {exc.message}") from exc
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError) as exc:
            raise ProviderError(f"{type(exc).__name__}: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderUnavailable(f"status {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderUnavailable(f"connection error: {exc}") from exc

        if response.stop_reason == "refusal":
            category = getattr(getattr(response, "stop_details", None), "category", None)
            raise ProviderRefusal(f"refused ({category or 'unspecified'})")
        if response.stop_reason == "max_tokens":
            raise ProviderTruncated("hit max_tokens")
        text = "".join(block.text for block in response.content if block.type == "text")
        usage = response.usage
        return ProviderResponse(
            text=text,
            model=response.model,
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", None) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", None) or 0,
            stop_reason=response.stop_reason or "end_turn",
            request_id=getattr(response, "_request_id", None),
        )


class OpenAICompatibleProvider:
    name = "openai"

    def __init__(self, base_url: str, api_key: str | None, *, timeout_s: float = 120.0) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout_s)

    async def complete(self, *, model: str, req: ModelRequest) -> ProviderResponse:
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": req.max_tokens,
            "messages": [{"role": "system", "content": req.system}]
            + [{"role": m.role, "content": m.content} for m in req.messages],
        }
        if req.output_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": req.output_schema.__name__,
                    "strict": True,
                    "schema": strict_json_schema(req.output_schema),
                },
            }
        try:
            response = await self._http.post(
                "/chat/completions", content=orjson.dumps(body), headers={"Content-Type": "application/json"}
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"connection error: {exc}") from exc
        if response.status_code == 429:
            raise ProviderRateLimited("rate limited", float(response.headers.get("retry-after", "30")))
        if response.status_code >= 500:
            raise ProviderUnavailable(f"status {response.status_code}")
        if response.status_code >= 400:
            raise ProviderError(f"status {response.status_code}: {response.text[:300]}")
        data = response.json()
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ProviderTruncated("hit max_tokens")
        usage = data.get("usage") or {}
        return ProviderResponse(
            text=choice["message"].get("content") or "",
            model=data.get("model", model),
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            stop_reason=choice.get("finish_reason") or "stop",
        )

    async def aclose(self) -> None:
        await self._http.aclose()


class OfflineProvider:
    name = "offline"

    async def complete(self, *, model: str, req: ModelRequest) -> ProviderResponse:
        text = generate_offline(req)
        prompt_chars = len(req.system) + sum(len(m.content) for m in req.messages)
        return ProviderResponse(text=text, model=model, input_tokens=prompt_chars // 4, output_tokens=len(text) // 4)
