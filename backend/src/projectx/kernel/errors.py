"""Application errors. The API layer renders these as RFC 9457 problem+json responses."""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    status: int = 400
    code: str = "bad_request"
    title: str = "Bad request"

    def __init__(
        self, detail: str | None = None, *, code: str | None = None, extra: dict[str, Any] | None = None
    ) -> None:
        super().__init__(detail or self.title)
        self.detail = detail or self.title
        if code:
            self.code = code
        self.extra = extra or {}


class NotFound(AppError):
    status, code, title = 404, "not_found", "Not found"


class Conflict(AppError):
    status, code, title = 409, "conflict", "Conflict"


class Forbidden(AppError):
    status, code, title = 403, "forbidden", "Forbidden"


class Unauthorized(AppError):
    status, code, title = 401, "unauthorized", "Sign in required"


class ValidationFailed(AppError):
    status, code, title = 422, "validation_failed", "Validation failed"


class RateLimited(AppError):
    status, code, title = 429, "rate_limited", "Too many requests"

    def __init__(self, detail: str | None = None, *, retry_after: int = 60) -> None:
        super().__init__(detail, extra={"retry_after": retry_after})
        self.retry_after = retry_after


class PolicyBlocked(AppError):
    status, code, title = 403, "policy_blocked", "Blocked by policy"


class QuotaExceeded(AppError):
    """A plan limit was reached. 402 tells the client an upgrade would unblock it."""

    status, code, title = 402, "quota_exceeded", "Plan limit reached"


class ExternalServiceError(AppError):
    status, code, title = 502, "external_service_error", "An external service failed"


class NotConfigured(AppError):
    status, code, title = 503, "not_configured", "This feature is not configured yet"
