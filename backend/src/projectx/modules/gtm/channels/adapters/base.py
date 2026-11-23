"""The adapter interface every platform implements, and the errors it may raise."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


@dataclass
class PublishRequest:
    platform: str
    format: str
    text: str
    title: str | None = None
    thread: list[str] = field(default_factory=list)
    media: list[dict[str, Any]] = field(default_factory=list)  # [{"url": ..., "kind": "image"|"video"}]
    ai_label: str = "none"
    idempotency_key: str = ""


@dataclass
class PublishResult:
    external_id: str
    url: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AccountInfo:
    external_id: str
    name: str
    handle: str | None = None
    avatar_url: str | None = None
    followers: int | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)


@dataclass
class AdapterContext:
    http: httpx.AsyncClient
    credentials: dict[str, Any]
    account_external_id: str
    settings: dict[str, Any]
    business_id: str
    followers: int = 0


class AdapterError(Exception):
    """A platform call failed. `retryable` tells the publisher whether trying again can help."""

    def __init__(self, message: str, *, retryable: bool = True, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class AuthExpired(AdapterError):
    def __init__(self, message: str = "The connection expired; reconnect the account") -> None:
        super().__init__(message, retryable=False)


class PlatformRejected(AdapterError):
    """The platform refused the content itself (policy, spec): retrying the same content won't help."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


class Adapter(Protocol):
    platform: str

    async def publish(self, ctx: AdapterContext, req: PublishRequest) -> PublishResult: ...

    async def verify(self, ctx: AdapterContext, external_id: str) -> bool: ...

    async def delete(self, ctx: AdapterContext, external_id: str) -> None: ...

    async def metrics(self, ctx: AdapterContext, external_ids: list[str]) -> dict[str, dict[str, float]]: ...

    async def account_info(self, ctx: AdapterContext) -> AccountInfo: ...


def raise_for_status(response: httpx.Response, platform: str) -> None:
    """Map HTTP failures to adapter errors in one place."""
    if response.status_code < 400:
        return
    detail = response.text[:300]
    if response.status_code in (401, 403) and ("token" in detail.lower() or response.status_code == 401):
        raise AuthExpired(f"{platform}: {detail}")
    if response.status_code == 429:
        retry = response.headers.get("retry-after")
        raise AdapterError(f"{platform} rate limit", retry_after=float(retry) if retry and retry.isdigit() else 900)
    if response.status_code >= 500:
        raise AdapterError(f"{platform} is having trouble ({response.status_code})")
    raise PlatformRejected(f"{platform} refused it ({response.status_code}): {detail}")
