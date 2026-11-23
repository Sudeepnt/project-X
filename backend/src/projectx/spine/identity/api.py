"""Sign up, sign in, sign out, email verification, password reset, invitations."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select

from projectx.api.deps import AuthDep, OptionalAuthDep, ServicesDep, SessionDep, client_ip
from projectx.kernel.errors import Forbidden, ValidationFailed
from projectx.kernel.time import utcnow
from projectx.settings import Settings
from projectx.spine.identity.models import User
from projectx.spine.identity.security import csrf_token_for
from projectx.spine.identity.service import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    authenticate,
    consume_email_token,
    create_session,
    issue_email_token,
    normalize_email,
    revoke_all_sessions,
    revoke_session,
    set_password,
    sign_up,
    user_view,
)
from projectx.spine.ledger.service import Actor, append
from projectx.spine.lifecycle import queue_email
from projectx.spine.mail import render
from projectx.spine.tenancy.models import ROLES, Membership
from projectx.spine.tenancy.service import business_view, list_businesses

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class SignupIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class TokenIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)


class ForgotIn(BaseModel):
    email: EmailStr


class ResetIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=1, max_length=256)


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


def _set_cookies(response: Response, token: str, token_hash: bytes, settings: Settings) -> str:
    max_age = settings.session_ttl_days * 86400
    csrf = csrf_token_for(token_hash, settings.secret_key.get_secret_value())
    common: dict[str, Any] = {
        "max_age": max_age,
        "secure": settings.secure_cookies,
        "samesite": "lax",
        "path": "/",
        "domain": settings.cookie_domain,
    }
    response.set_cookie(SESSION_COOKIE, token, httponly=True, **common)
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, **common)
    return csrf


def _clear_cookies(response: Response, settings: Settings) -> None:
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(name, path="/", domain=settings.cookie_domain)


async def _me_payload(session: SessionDep, user: User, csrf: str) -> dict[str, Any]:
    businesses = await list_businesses(session, user.id)
    return {
        "user": user_view(user),
        "businesses": [business_view(b, role) for b, role in businesses],
        "csrf_token": csrf,
    }


async def _send_verification(session: SessionDep, user: User, settings: Settings) -> None:
    token = await issue_email_token(session, user_id=user.id, purpose="verify_email", ttl=timedelta(days=2))
    link = f"{settings.web_url}/verify-email?token={token}"
    await queue_email(
        session,
        to=user.email,
        subject="Confirm your email",
        html=render(
            "email/action.html",
            title="Confirm your email",
            name=user.name,
            body="Confirm this address so we can reach you about your business.",
            action_label="Confirm email",
            action_url=link,
            app_name=settings.app_name,
        ),
        text=f"Hi {user.name},\n\nConfirm your email: {link}\n\nThis link expires in 2 days.",
    )


@router.post("/signup", status_code=status.HTTP_201_CREATED)
async def signup(
    body: SignupIn, request: Request, response: Response, session: SessionDep, services: ServicesDep
) -> dict[str, Any]:
    settings = services.settings
    if not settings.signup_enabled:
        raise Forbidden("Sign-ups are closed right now.")
    ip = client_ip(request)
    user = await sign_up(session, email=body.email, name=body.name, password=body.password, ip=ip, settings=settings)
    token, user_session = await create_session(
        session, user, user_agent=request.headers.get("user-agent"), ip=ip, settings=settings
    )
    await _send_verification(session, user, settings)
    csrf = _set_cookies(response, token, user_session.token_hash, settings)
    return await _me_payload(session, user, csrf)


@router.post("/login")
async def login(
    body: LoginIn, request: Request, response: Response, session: SessionDep, services: ServicesDep
) -> dict[str, Any]:
    ip = client_ip(request)
    user = await authenticate(session, email=body.email, password=body.password, ip=ip, settings=services.settings)
    token, user_session = await create_session(
        session, user, user_agent=request.headers.get("user-agent"), ip=ip, settings=services.settings
    )
    csrf = _set_cookies(response, token, user_session.token_hash, services.settings)
    return await _me_payload(session, user, csrf)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(auth: AuthDep, response: Response, session: SessionDep, services: ServicesDep) -> Response:
    await revoke_session(session, auth.session.id, auth.user.id)
    _clear_cookies(response, services.settings)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me")
async def me(auth: OptionalAuthDep, session: SessionDep, services: ServicesDep) -> dict[str, Any]:
    if auth is None:
        return {"user": None, "businesses": [], "csrf_token": None}
    user = await session.get(User, auth.user.id)
    assert user is not None
    csrf = csrf_token_for(auth.token_hash, services.settings.secret_key.get_secret_value())
    return await _me_payload(session, user, csrf)


@router.post("/verify-email")
async def verify_email(body: TokenIn, session: SessionDep) -> dict[str, Any]:
    row = await consume_email_token(session, body.token, "verify_email")
    user = await session.get(User, row.user_id) if row.user_id else None
    if user is None:
        raise ValidationFailed("This link is invalid or has expired.")
    user.email_verified_at = user.email_verified_at or utcnow()
    return {"verified": True}


@router.post("/verify-email/resend", status_code=status.HTTP_202_ACCEPTED)
async def resend_verification(auth: AuthDep, session: SessionDep, services: ServicesDep) -> dict[str, Any]:
    user = await session.get(User, auth.user.id)
    if user is not None and user.email_verified_at is None:
        await _send_verification(session, user, services.settings)
    return {"sent": True}


@router.post("/password/forgot", status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(body: ForgotIn, session: SessionDep, services: ServicesDep) -> dict[str, Any]:
    # Same answer whether or not the account exists, so this can't be used to find accounts.
    user = await session.scalar(select(User).where(User.email == normalize_email(body.email), User.is_active.is_(True)))
    if user is not None:
        settings = services.settings
        token = await issue_email_token(session, user_id=user.id, purpose="reset_password", ttl=timedelta(hours=1))
        link = f"{settings.web_url}/reset-password?token={token}"
        await queue_email(
            session,
            to=user.email,
            subject="Reset your password",
            html=render(
                "email/action.html",
                title="Reset your password",
                name=user.name,
                body="Someone asked to reset the password for this account. If it wasn't you, ignore this email.",
                action_label="Choose a new password",
                action_url=link,
                app_name=settings.app_name,
            ),
            text=f"Reset your password: {link}\n\nThis link expires in 1 hour.",
        )
    return {"sent": True}


@router.post("/password/reset")
async def reset_password(body: ResetIn, session: SessionDep) -> dict[str, Any]:
    row = await consume_email_token(session, body.token, "reset_password")
    user = await session.get(User, row.user_id) if row.user_id else None
    if user is None:
        raise ValidationFailed("This link is invalid or has expired.")
    await set_password(session, user, body.password)
    await revoke_all_sessions(session, user.id)
    return {"reset": True}


@router.post("/password/change")
async def change_password(
    body: ChangePasswordIn, auth: AuthDep, session: SessionDep, services: ServicesDep
) -> dict[str, Any]:
    user = await authenticate(
        session, email=auth.user.email, password=body.current_password, ip=None, settings=services.settings
    )
    await set_password(session, user, body.new_password)
    await revoke_all_sessions(session, user.id, except_id=auth.session.id)
    return {"changed": True}


class AcceptInviteIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)


@router.post("/invites/accept")
async def accept_invite(body: AcceptInviteIn, auth: AuthDep, session: SessionDep) -> dict[str, Any]:
    from projectx.db.database import set_tenant

    row = await consume_email_token(session, body.token, "invite")
    business_id = uuid.UUID(row.payload["business_id"])
    role = row.payload.get("role", "editor")
    if role not in ROLES or role == "owner":
        role = "editor"
    existing = await session.get(Membership, (auth.user.id, business_id))
    if existing is None:
        session.add(Membership(user_id=auth.user.id, business_id=business_id, role=role))
        await set_tenant(session, business_id)
        await append(
            session,
            business_id=business_id,
            type="spine.member.added",
            actor=Actor.user(auth.user.id),
            data={"user_id": str(auth.user.id), "role": role},
        )
    return {"business_id": str(business_id), "role": existing.role if existing else role}
