"""The practice platform: behaves like a real one, with failures injected on purpose.

"A practice company before a real one" (blueprint §7): every pack runs against this first.
Failures arrive at the MORPHEUS levels (5%, 8%, 15%, 30%): timeouts, rate limits and refusals.
It is stateless and deterministic: ids, verification and metrics derive from hashes, so any
worker process gives the same answers.
"""

from __future__ import annotations

import hashlib
import time

from projectx.modules.gtm.channels.adapters.base import (
    AccountInfo,
    AdapterContext,
    AdapterError,
    PlatformRejected,
    PublishRequest,
    PublishResult,
)

FAILURE_LEVELS = (0.0, 0.05, 0.08, 0.15, 0.30)


def _unit(*parts: object) -> float:
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


class SimulatorAdapter:
    platform = "simulator"

    def __init__(self, platform: str) -> None:
        self.platform = platform

    def _failure_rate(self, ctx: AdapterContext) -> float:
        return float(ctx.settings.get("failure_rate", 0.0))

    async def publish(self, ctx: AdapterContext, req: PublishRequest) -> PublishResult:
        roll = _unit("publish", ctx.business_id, req.idempotency_key, int(time.time() // 300))
        rate = self._failure_rate(ctx)
        if roll < rate * 0.5:
            raise AdapterError(f"{self.platform} (practice) timed out")
        if roll < rate * 0.8:
            raise AdapterError(f"{self.platform} (practice) rate limit", retry_after=60)
        if roll < rate:
            raise PlatformRejected(f"{self.platform} (practice) refused the post")
        external_id = "sim_" + hashlib.sha256(f"{ctx.business_id}:{req.idempotency_key}".encode()).hexdigest()[:16]
        return PublishResult(
            external_id=external_id, url=f"https://practice.projectx.local/{self.platform}/{external_id}"
        )

    async def verify(self, ctx: AdapterContext, external_id: str) -> bool:
        return _unit("verify", external_id) >= self._failure_rate(ctx) * 0.2

    async def delete(self, ctx: AdapterContext, external_id: str) -> None:
        return None

    async def metrics(self, ctx: AdapterContext, external_ids: list[str]) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        base = max(ctx.followers, 200)
        for external_id in external_ids:
            quality = _unit("quality", external_id)  # some posts simply land better
            reach = round(base * (0.03 + 0.25 * quality**2))
            engagements = round(reach * (0.01 + 0.07 * quality))
            out[external_id] = {
                "impressions": round(reach * 1.4),
                "reach": reach,
                "engagements": engagements,
                "likes": round(engagements * 0.7),
                "comments": round(engagements * 0.1),
                "shares": round(engagements * 0.1),
                "saves": round(engagements * 0.1),
                "clicks": round(reach * (0.002 + 0.015 * quality)),
            }
        return out

    async def account_info(self, ctx: AdapterContext) -> AccountInfo:
        return AccountInfo(
            external_id=ctx.account_external_id,
            name=f"Practice {self.platform}",
            handle=f"practice_{self.platform}",
            followers=ctx.followers,
            capabilities={"practice": True, "audited": True},
        )
