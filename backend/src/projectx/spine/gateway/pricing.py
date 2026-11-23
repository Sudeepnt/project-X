"""Model prices in USD per million tokens, which equals micro-USD per token.

These are defaults; prices change, so override them with ``PX_LLM_PRICES`` (JSON:
``{"model": [input, output, cache_read, cache_write]}``) and check providers' pages on the day
you rely on them. Claude prices are first-party API rates as of 2026-11; cache writes use the
standard 1.25x input rate for the 5-minute cache.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Price:
    input: float
    output: float
    cache_read: float
    cache_write: float


DEFAULT_PRICES: dict[str, Price] = {
    "claude-opus-5-5": Price(4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": Price(2.00, 10.00, 0.10, 2.50),
    "claude-haiku-5-5": Price(0.10, 0.50, 0.01, 0.125),
}


def _load_overrides() -> dict[str, Price]:
    raw = os.environ.get("PX_LLM_PRICES")
    if not raw:
        return {}
    data = json.loads(raw)
    return {model: Price(*[float(x) for x in values]) for model, values in data.items()}


_PRICES = {**DEFAULT_PRICES, **_load_overrides()}


def price_for(model: str) -> Price | None:
    return _PRICES.get(model)


def cost_micro_usd(
    model: str, *, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0, cache_write_tokens: int = 0
) -> int:
    price = price_for(model)
    if price is None:
        return 0
    total = (
        input_tokens * price.input
        + output_tokens * price.output
        + cache_read_tokens * price.cache_read
        + cache_write_tokens * price.cache_write
    )
    return math.ceil(total)


def worst_case_micro_usd(model: str, *, prompt_chars: int, max_tokens: int) -> int:
    """Upper bound used by the daily cap before a call is made (~4 chars per token)."""
    return cost_micro_usd(model, input_tokens=prompt_chars // 4 + 1, output_tokens=max_tokens)
