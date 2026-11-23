"""The FastAPI application."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import Scope

from projectx import __version__
from projectx.api.errors import install_error_handlers, problem
from projectx.api.middleware import RequestContextMiddleware
from projectx.logconfig import configure_logging
from projectx.services import Services
from projectx.settings import Settings, get_settings
from projectx.spine.api import public_router as spine_public_router
from projectx.spine.api import router as spine_router
from projectx.spine.identity.api import router as identity_router
from projectx.spine.modules import load_modules
from projectx.spine.tenancy.api import router as tenancy_router

_RESERVED_PREFIXES = ("api/", "l/", "p/", "u/", "files/", "webhooks/", "healthz", "readyz", "public/")


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    loaded = load_modules()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        svc = services or Services.create(settings)
        app.state.services = svc
        try:
            yield
        finally:
            if services is None:
                await svc.close()

    app = FastAPI(
        title=f"{settings.app_name} API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    install_error_handlers(app)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(RequestContextMiddleware, settings=settings)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> Response:
        try:
            async with request.app.state.services.db.session() as session:
                await session.execute(text("SELECT 1"))
        except Exception:
            return problem(503, "not_ready", "Database unavailable")
        return JSONResponse({"status": "ready"})

    @app.get("/files/{key:path}", include_in_schema=False)
    async def files(key: str, e: int, s: str, request: Request) -> Response:
        blobs = request.app.state.services.blobs
        if not blobs.verify(key, e, s):
            return problem(403, "forbidden", "This file link is invalid or has expired")
        data, content_type = await blobs.get(key)
        return Response(data, media_type=content_type, headers={"Cache-Control": "private, max-age=3600"})

    app.include_router(identity_router)
    app.include_router(tenancy_router)
    app.include_router(spine_router)
    app.include_router(spine_public_router)
    for module in loaded:
        for router in module.routers:
            app.include_router(router)
        for router in module.public_routers:
            app.include_router(router)

    _mount_web(app)
    return app


class ImmutableStaticFiles(StaticFiles):
    """Build assets have content hashes in their names, so browsers may cache them forever."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def _mount_web(app: FastAPI) -> None:
    """Serve the built cockpit (web/dist) from the same origin, with client-side routing."""
    dist = Path(__file__).resolve().parents[4] / "web" / "dist"
    index = dist / "index.html"
    if not index.exists():
        return
    if (dist / "assets").exists():
        app.mount("/assets", ImmutableStaticFiles(directory=dist / "assets"), name="assets")

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def spa(path: str) -> Response:
        if path.startswith(_RESERVED_PREFIXES):
            return problem(404, "not_found", "Not found")
        candidate = (dist / path).resolve()
        if path and dist in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
