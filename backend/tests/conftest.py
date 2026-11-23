"""Test harness.

Tests run against a real PostgreSQL 18 database (``projectx_test``) as the same non-superuser role
the application uses, so row-level security is actually exercised. The schema is rebuilt with
Alembic once per session (downgrade to base, upgrade to head), which also proves both
directions of every migration work. Tests isolate themselves by creating their own users and
businesses; nothing is truncated (the ledger forbids it).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config

TEST_DB_URL = os.environ.get(
    "PX_TEST_DATABASE_URL", "postgresql+asyncpg://projectx:projectx@localhost:5432/projectx_test"
)
os.environ.setdefault("PX_ENV", "test")
os.environ["PX_DATABASE_URL"] = TEST_DB_URL
os.environ.setdefault("PX_LLM_SMALL", "offline:draft-small")
os.environ.setdefault("PX_LLM_MID", "offline:draft-mid")
os.environ.setdefault("PX_LLM_FRONTIER", "offline:draft-frontier")

from projectx.api.app import create_app  # noqa: E402
from projectx.services import Services  # noqa: E402
from projectx.settings import Settings  # noqa: E402

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _alembic_config() -> Config:
    cfg = Config(os.path.join(BACKEND_DIR, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "migrations"))
    cfg.attributes["url"] = TEST_DB_URL
    return cfg


@pytest.fixture(scope="session", autouse=True)
def _schema() -> None:
    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(
        env="test",
        database_url=TEST_DB_URL,
        scheduler_enabled=False,
        db_pool_size=5,
        llm_small="offline:draft-small",
        llm_mid="offline:draft-mid",
        llm_frontier="offline:draft-frontier",
        default_plan="scale",
        auth_signups_per_hour_per_ip=100_000,
        auth_logins_per_15m_per_ip=100_000,
    )


@pytest.fixture(scope="session")
async def services(settings: Settings) -> AsyncIterator[Services]:
    svc = Services.create(settings)
    sent: list[dict[str, Any]] = []

    async def fake_send(**kwargs: Any) -> str:
        sent.append(kwargs)
        return f"<{uuid.uuid4().hex}@test>"

    svc.mailer.send = fake_send  # type: ignore[method-assign]
    svc.sent_emails = sent  # type: ignore[attr-defined]
    yield svc
    await svc.close()


@pytest.fixture(scope="session")
def app(settings: Settings, services: Services) -> Any:
    application = create_app(settings, services)
    application.state.services = services
    return application


@pytest.fixture
async def client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class Session:
    """A signed-in test client with its CSRF token."""

    def __init__(self, client: httpx.AsyncClient, csrf: str, user: dict[str, Any]) -> None:
        self.client = client
        self.csrf = csrf
        self.user = user

    def _h(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf}

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        return await self.client.get(url, **kw)

    async def post(self, url: str, json: Any = None, **kw: Any) -> httpx.Response:
        return await self.client.post(url, json=json, headers=self._h(), **kw)

    async def patch(self, url: str, json: Any = None, **kw: Any) -> httpx.Response:
        return await self.client.patch(url, json=json, headers=self._h(), **kw)

    async def put(self, url: str, json: Any = None, **kw: Any) -> httpx.Response:
        return await self.client.put(url, json=json, headers=self._h(), **kw)

    async def delete(self, url: str, **kw: Any) -> httpx.Response:
        return await self.client.delete(url, headers=self._h(), **kw)


def unique_email(prefix: str = "founder") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}@example.com"


async def sign_up(
    client: httpx.AsyncClient,
    *,
    email: str | None = None,
    name: str = "Ada Founder",
    password: str = "correct horse battery staple",
) -> Session:
    res = await client.post(
        "/api/v1/auth/signup", json={"name": name, "email": email or unique_email(), "password": password}
    )
    assert res.status_code == 201, res.text
    body = res.json()
    return Session(client, body["csrf_token"], body["user"])


async def new_business(session: Session, **fields: Any) -> dict[str, Any]:
    payload = {
        "name": fields.pop("name", f"Biz {uuid.uuid4().hex[:6]}"),
        "business_model": "media",
        "timezone": "Asia/Kolkata",
        "currency": "INR",
        **fields,
    }
    res = await session.post("/api/v1/businesses", json=payload)
    assert res.status_code == 201, res.text
    body: dict[str, Any] = res.json()
    return body


@pytest.fixture
async def founder(client: httpx.AsyncClient) -> Session:
    return await sign_up(client)


@pytest.fixture
async def business(founder: Session) -> dict[str, Any]:
    return await new_business(founder)
