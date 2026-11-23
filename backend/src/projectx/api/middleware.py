"""Pure-ASGI middleware (no BaseHTTPMiddleware overhead): request ids, access logs, security
headers, and an Origin check that stops cross-site form posts against cookie sessions."""

from __future__ import annotations

import time
import uuid
from urllib.parse import urlsplit

import orjson
import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from projectx.settings import Settings

log = structlog.get_logger("http")

_UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_SKIP_LOG = frozenset({"/healthz", "/readyz"})

_CSP_HTML = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob: https:; media-src 'self' blob: https:; font-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'"
)


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings
        self.allowed_origins = {
            _origin_of(settings.base_url),
            _origin_of(settings.web_url),
            *(_origin_of(o) for o in settings.trusted_origins),
        }
        self.hsts = not settings.is_dev

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        request_id = headers.get("x-request-id", "")[:64] or uuid.uuid4().hex
        method: str = scope["method"]
        path: str = scope["path"]
        structlog.contextvars.bind_contextvars(request_id=request_id)

        if method in _UNSAFE and path.startswith("/api/"):
            origin = headers.get("origin")
            if origin and origin not in self.allowed_origins:
                await self._reject(send, request_id)
                structlog.contextvars.clear_contextvars()
                return

        started = time.perf_counter()
        status_holder = {"status": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                raw = list(message.get("headers", []))
                existing = {k.lower() for k, _ in raw}
                content_type = next((v for k, v in raw if k.lower() == b"content-type"), b"")
                extra = [
                    (b"x-request-id", request_id.encode()),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"strict-origin-when-cross-origin"),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
                    (b"cross-origin-opener-policy", b"same-origin"),
                ]
                if b"x-frame-options" not in existing:
                    extra.append((b"x-frame-options", b"DENY"))
                if content_type.startswith(b"text/html") and b"content-security-policy" not in existing:
                    extra.append((b"content-security-policy", _CSP_HTML.encode()))
                if self.hsts:
                    extra.append((b"strict-transport-security", b"max-age=63072000; includeSubDomains; preload"))
                if path.startswith("/api/") and b"cache-control" not in existing:
                    extra.append((b"cache-control", b"no-store"))
                message["headers"] = raw + extra
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if path not in _SKIP_LOG:
                log.info(
                    "request",
                    method=method,
                    path=path,
                    status=status_holder["status"],
                    ms=round((time.perf_counter() - started) * 1000, 1),
                )
            structlog.contextvars.clear_contextvars()

    @staticmethod
    async def _reject(send: Send, request_id: str) -> None:
        body = orjson.dumps(
            {
                "type": "https://projectx.dev/problems/bad_origin",
                "title": "Request blocked",
                "status": 403,
                "code": "bad_origin",
                "detail": "This request came from a site that is not allowed.",
            }
        )
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/problem+json"),
                    (b"x-request-id", request_id.encode()),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
