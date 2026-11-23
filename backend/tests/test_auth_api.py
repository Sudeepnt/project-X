from __future__ import annotations

from typing import Any

import httpx

from tests.conftest import new_business, sign_up, unique_email


async def test_signup_sets_session_and_returns_me(client: httpx.AsyncClient) -> None:
    email = unique_email()
    founder = await sign_up(client, email=email)
    assert founder.user["email"] == email
    me = (await client.get("/api/v1/auth/me")).json()
    assert me["user"]["email"] == email
    assert me["csrf_token"] == founder.csrf


async def test_duplicate_signup_is_a_conflict(client: httpx.AsyncClient) -> None:
    email = unique_email()
    await sign_up(client, email=email)
    res = await client.post(
        "/api/v1/auth/signup", json={"name": "Again", "email": email, "password": "another long password"}
    )
    assert res.status_code == 409
    assert res.headers["content-type"].startswith("application/problem+json")


async def test_weak_password_is_rejected(client: httpx.AsyncClient) -> None:
    res = await client.post("/api/v1/auth/signup", json={"name": "W", "email": unique_email(), "password": "short"})
    assert res.status_code == 422
    assert "characters" in res.json()["detail"]


async def test_login_logout_and_wrong_password(app: Any) -> None:
    email = unique_email()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        session = await sign_up(c, email=email)
        assert (await session.post("/api/v1/auth/logout")).status_code == 204
        assert (await c.get("/api/v1/auth/me")).json()["user"] is None
        bad = await c.post("/api/v1/auth/login", json={"email": email, "password": "wrong password here"})
        assert bad.status_code == 401
        good = await c.post("/api/v1/auth/login", json={"email": email, "password": "correct horse battery staple"})
        assert good.status_code == 200
        assert good.json()["user"]["email"] == email


async def test_state_changes_require_csrf_token(founder: Any) -> None:
    res = await founder.client.post("/api/v1/businesses", json={"name": "No CSRF"})
    assert res.status_code == 403
    assert res.json()["code"] == "csrf_failed"


async def test_cross_site_origin_is_blocked(founder: Any) -> None:
    res = await founder.client.post(
        "/api/v1/businesses",
        json={"name": "Evil"},
        headers={"X-CSRF-Token": founder.csrf, "Origin": "https://evil.example"},
    )
    assert res.status_code == 403
    assert res.json()["code"] == "bad_origin"


async def test_business_is_private_to_its_members(app: Any) -> None:
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as a,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as b,
    ):
        owner = await sign_up(a)
        stranger = await sign_up(b)
        biz = await new_business(owner, name="Bought")
        assert biz["role"] == "owner"
        assert (await owner.get(f"/api/v1/b/{biz['id']}")).status_code == 200
        assert (await stranger.get(f"/api/v1/b/{biz['id']}")).status_code == 404
        assert (await stranger.get(f"/api/v1/b/{biz['id']}/decisions")).status_code == 404


async def test_verification_email_is_queued_and_link_works(founder: Any, services: Any) -> None:
    from projectx.spine.jobs.worker import Worker

    await Worker(services).run_until_idle()
    sent = [m for m in services.sent_emails if m["to"] == founder.user["email"]]
    assert sent and sent[-1]["subject"] == "Confirm your email"
    token = sent[-1]["text"].split("token=")[1].split()[0]
    res = await founder.post("/api/v1/auth/verify-email", json={"token": token})
    assert res.status_code == 200
    assert (await founder.get("/api/v1/auth/me")).json()["user"]["email_verified"] is True
    again = await founder.post("/api/v1/auth/verify-email", json={"token": token})
    assert again.status_code == 422


async def test_security_headers_present(client: httpx.AsyncClient) -> None:
    res = await client.get("/healthz")
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["x-frame-options"] == "DENY"
    assert "x-request-id" in res.headers
