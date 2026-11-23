"""Job handlers and periodic schedules, registered by the spine and by modules."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from projectx.services import Services


class RetryLater(Exception):
    """Re-run the job after `delay_s` without counting it as a failed attempt
    (rate limits, a daily compute cap, a platform asking us to slow down)."""

    def __init__(self, delay_s: float, reason: str = "") -> None:
        super().__init__(reason or f"retry in {delay_s:.0f}s")
        self.delay_s = delay_s
        self.reason = reason


class PermanentFailure(Exception):
    """Retrying cannot help (bad input, revoked access). The job goes straight to dead."""


@dataclass
class JobContext:
    services: Services
    job_id: uuid.UUID
    kind: str
    attempt: int
    business_id: uuid.UUID | None
    payload: dict[str, Any]
    log: Any = field(default_factory=lambda: structlog.get_logger("job"))

    def require_business(self) -> uuid.UUID:
        if self.business_id is None:
            raise PermanentFailure(f"job {self.kind} needs a business")
        return self.business_id


Handler = Callable[[JobContext], Awaitable[dict[str, Any] | None]]


@dataclass(frozen=True)
class JobSpec:
    kind: str
    handler: Handler
    max_attempts: int = 8
    timeout_s: float = 300
    priority: int = 100


@dataclass(frozen=True)
class PeriodicSpec:
    name: str
    every: timedelta
    fn: Callable[[Services, datetime], Awaitable[None]]


_JOBS: dict[str, JobSpec] = {}
_PERIODIC: dict[str, PeriodicSpec] = {}


def job(
    kind: str, *, max_attempts: int = 8, timeout_s: float = 300, priority: int = 100
) -> Callable[[Handler], Handler]:
    def decorator(fn: Handler) -> Handler:
        if kind in _JOBS and _JOBS[kind].handler is not fn:
            raise ValueError(f"job kind {kind!r} registered twice")
        _JOBS[kind] = JobSpec(kind, fn, max_attempts, timeout_s, priority)
        return fn

    return decorator


def periodic(
    name: str, every: timedelta
) -> Callable[[Callable[[Services, datetime], Awaitable[None]]], Callable[[Services, datetime], Awaitable[None]]]:
    def decorator(
        fn: Callable[[Services, datetime], Awaitable[None]],
    ) -> Callable[[Services, datetime], Awaitable[None]]:
        _PERIODIC[name] = PeriodicSpec(name, every, fn)
        return fn

    return decorator


def get_job(kind: str) -> JobSpec | None:
    return _JOBS.get(kind)


def all_jobs() -> list[JobSpec]:
    return sorted(_JOBS.values(), key=lambda s: s.kind)


def periodic_specs() -> list[PeriodicSpec]:
    return sorted(_PERIODIC.values(), key=lambda s: s.name)
