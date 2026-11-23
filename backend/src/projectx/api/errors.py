"""Every error leaves the API as RFC 9457 problem+json, with a stable machine-readable code."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from projectx.kernel.errors import AppError, RateLimited

log = structlog.get_logger("api")

PROBLEM_JSON = "application/problem+json"


def problem(status: int, code: str, title: str, detail: str | None = None, **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"https://projectx.dev/problems/{code}",
        "title": title,
        "status": status,
        "code": code,
    }
    if detail:
        body["detail"] = detail
    body.update({k: v for k, v in extra.items() if v is not None})
    headers = {}
    if "retry_after" in extra and extra["retry_after"] is not None:
        headers["Retry-After"] = str(extra["retry_after"])
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        if exc.status >= 500:
            log.error("app_error", code=exc.code, detail=exc.detail, path=request.url.path)
        extra = dict(exc.extra)
        if isinstance(exc, RateLimited):
            extra["retry_after"] = exc.retry_after
        return problem(exc.status, exc.code, exc.title, exc.detail, **extra)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"field": ".".join(str(p) for p in err.get("loc", ())[1:]) or None, "message": err.get("msg", "invalid")}
            for err in exc.errors()
        ]
        first = errors[0]["message"] if errors else None
        return problem(422, "validation_failed", "Validation failed", first, errors=errors)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed", 401: "unauthorized", 403: "forbidden"}.get(
            exc.status_code, "http_error"
        )
        return problem(exc.status_code, code, str(exc.detail) if exc.detail else code.replace("_", " ").capitalize())

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error", path=request.url.path)
        return problem(
            500, "internal_error", "Something went wrong on our side", "The error was logged. Please try again."
        )
