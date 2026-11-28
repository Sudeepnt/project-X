from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

Rung = Literal["small", "mid", "frontier"]
RUNG_ORDER: tuple[Rung, ...] = ("small", "mid", "frontier")
Effort = Literal["low", "medium", "high"]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Literal["user", "assistant"]
    content: str


@dataclass
class ModelRequest:
    """One unit of model work. `task` names the job for routing, caching and cost reports."""

    task: str
    system: str
    messages: list[ChatMessage]
    max_tokens: int = 4000
    output_schema: type[BaseModel] | None = None
    effort: Effort = "low"
    rung: Rung = "small"
    max_rung: Rung = "frontier"
    cache: bool = True
    # Hard jobs (coding, long multi-step reasoning) may use the "hard" model; everything else stays on open models.
    hard: bool = False
    # Structured inputs. Logged with the call and used by the offline provider.
    context: dict[str, Any] = field(default_factory=dict)
    # Returns a problem description when the answer is not good enough; the router then climbs.
    verifier: Callable[[ModelResult], str | None] | None = None


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    stop_reason: str = "end_turn"
    request_id: str | None = None


@dataclass
class ModelResult:
    text: str
    parsed: BaseModel | None
    provider: str
    model: str
    rung: Rung
    cost_micro_usd: int
    cached: bool
    attempts: int
    call_ids: list[uuid.UUID] = field(default_factory=list)

    def parsed_as[T: BaseModel](self, schema: type[T]) -> T:
        if not isinstance(self.parsed, schema):
            raise TypeError(f"expected {schema.__name__}, got {type(self.parsed).__name__}")
        return self.parsed


class ProviderError(Exception):
    status = "error"


class ProviderRefusal(ProviderError):
    status = "refused"


class ProviderTruncated(ProviderError):
    status = "truncated"


class ProviderUnavailable(ProviderError):
    status = "unavailable"


class ProviderRateLimited(ProviderError):
    status = "rate_limited"

    def __init__(self, message: str, retry_after: float = 30.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class GatewayError(Exception):
    pass


class ComputeCapReached(GatewayError):
    """The business hit its daily compute cap. Work waits; it never overspends."""

    def __init__(self, spent_micro_usd: int, cap_micro_usd: int) -> None:
        super().__init__(f"daily compute cap reached ({spent_micro_usd}/{cap_micro_usd} micro-USD)")
        self.spent_micro_usd = spent_micro_usd
        self.cap_micro_usd = cap_micro_usd


class ModelsExhausted(GatewayError):
    """Every rung failed or was rejected by its check."""


class ModelsRateLimited(GatewayError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"model providers are rate limiting; retry in {retry_after:.0f}s")
        self.retry_after = retry_after
