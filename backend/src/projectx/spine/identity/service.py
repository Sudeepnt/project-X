"""Accounts and sessions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.errors import Conflict, Unauthorized, ValidationFailed
from projectx.kernel.ids import token_urlsafe
from projectx.kernel.time import utcnow
from projectx.settings import Settings
from projectx.spine.identity.models import EmailToken, User, UserSession
from projectx.spine.identity.security import (
    hash_ip,
    hash_password,
    hash_token,
    password_problem,
    throttle,
    verify_password,
)

SESSION_COOKIE = "px_session"
CSRF_COOKIE = "px_csrf"
LOCK_AFTER_FAILURES = 10
LOCK_FOR = timedelta(minutes=15)


@dataclass(frozen=True)
class AuthContext:
    user: User
    session: UserSession
    token_hash: bytes


def normalize_email(email: str) -> str:
    return email.strip().lower()


async def sign_up(
    session: AsyncSession, *, email: str, name: str, password: str, ip: str | None, settings: Settings
) -> User:
    email = normalize_email(email)
    if ip:
        await throttle(
            session,
            f"signup:{hash_ip(ip, settings.secret_key.get_secret_value())}",
            limit=settings.auth_signups_per_hour_per_ip,
            window=timedelta(hours=1),
        )
    problem = password_problem(password, email=email, name=name)
    if problem:
        raise ValidationFailed(problem, extra={"field": "password"})
    existing = await session.scalar(select(User.id).where(User.email == email))
    if existing is not None:
        raise Conflict("An account with this email already exists. Try signing in.", extra={"field": "email"})
    user = User(email=email, name=name.strip()[:120], password_hash=hash_password(password))
    session.add(user)
    await session.flush()
    return user


async def authenticate(session: AsyncSession, *, email: str, password: str, ip: str | None, settings: Settings) -> User:
    email = normalize_email(email)
    secret = settings.secret_key.get_secret_value()
    if ip:
        await throttle(
            session,
            f"login:ip:{hash_ip(ip, secret)}",
            limit=settings.auth_logins_per_15m_per_ip,
            window=timedelta(minutes=15),
        )
    await throttle(
        session,
        f"login:email:{hash_token(email).hex()[:32]}",
        limit=settings.auth_logins_per_15m_per_email,
        window=timedelta(minutes=15),
    )

    user = await session.scalar(select(User).where(User.email == email))
    ok, new_hash = verify_password(user.password_hash if user else None, password)
    now = utcnow()
    if user is not None and user.locked_until is not None and user.locked_until > now:
        raise Unauthorized("This account is locked for a few minutes after too many attempts.")
    if not ok or user is None or not user.is_active:
        if user is not None:
            user.failed_logins += 1
            if user.failed_logins >= LOCK_AFTER_FAILURES:
                user.locked_until = now + LOCK_FOR
                user.failed_logins = 0
        raise Unauthorized("That email and password don't match.")
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    if new_hash:
        user.password_hash = new_hash
    return user


async def create_session(
    session: AsyncSession, user: User, *, user_agent: str | None, ip: str | None, settings: Settings
) -> tuple[str, UserSession]:
    token = token_urlsafe(32)
    row = UserSession(
        user_id=user.id,
        token_hash=hash_token(token),
        expires_at=utcnow() + timedelta(days=settings.session_ttl_days),
        user_agent=(user_agent or "")[:300] or None,
        ip_hash=hash_ip(ip, settings.secret_key.get_secret_value()),
    )
    session.add(row)
    await session.flush()
    return token, row


async def resolve_session(session: AsyncSession, token: str, settings: Settings) -> AuthContext | None:
    token_hash = hash_token(token)
    row = (
        await session.execute(
            select(UserSession, User)
            .join(User, User.id == UserSession.user_id)
            .where(
                UserSession.token_hash == token_hash,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > utcnow(),
                User.is_active.is_(True),
            )
        )
    ).first()
    if row is None:
        return None
    user_session, user = row
    now = utcnow()
    ttl = timedelta(days=settings.session_ttl_days)
    if now - user_session.last_seen_at > timedelta(minutes=5):
        values: dict[str, Any] = {"last_seen_at": now}
        if user_session.expires_at - now < ttl / 2:  # sliding expiry for active users
            values["expires_at"] = now + ttl
        await session.execute(update(UserSession).where(UserSession.id == user_session.id).values(**values))
    return AuthContext(user=user, session=user_session, token_hash=token_hash)


async def revoke_session(session: AsyncSession, session_id: uuid.UUID, user_id: uuid.UUID) -> None:
    await session.execute(
        update(UserSession)
        .where(UserSession.id == session_id, UserSession.user_id == user_id)
        .values(revoked_at=utcnow())
    )


async def revoke_all_sessions(session: AsyncSession, user_id: uuid.UUID, *, except_id: uuid.UUID | None = None) -> None:
    stmt = update(UserSession).where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
    if except_id is not None:
        stmt = stmt.where(UserSession.id != except_id)
    await session.execute(stmt.values(revoked_at=utcnow()))


async def issue_email_token(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    purpose: str,
    ttl: timedelta,
    payload: dict[str, Any] | None = None,
) -> str:
    token = token_urlsafe(32)
    session.add(
        EmailToken(
            user_id=user_id,
            purpose=purpose,
            token_hash=hash_token(token),
            payload=payload or {},
            expires_at=utcnow() + ttl,
        )
    )
    await session.flush()
    return token


async def consume_email_token(session: AsyncSession, token: str, purpose: str) -> EmailToken:
    row = await session.scalar(
        select(EmailToken)
        .where(EmailToken.token_hash == hash_token(token), EmailToken.purpose == purpose)
        .with_for_update()
    )
    if row is None or row.used_at is not None or row.expires_at < utcnow():
        raise ValidationFailed("This link is invalid or has expired. Request a new one.")
    row.used_at = utcnow()
    return row


async def set_password(session: AsyncSession, user: User, password: str) -> None:
    problem = password_problem(password, email=user.email, name=user.name)
    if problem:
        raise ValidationFailed(problem, extra={"field": "password"})
    user.password_hash = hash_password(password)
    user.failed_logins = 0
    user.locked_until = None


def user_view(user: User) -> dict[str, Any]:
    return {
        "id": str(user.id),
        "email": user.email,
        "name": user.name,
        "email_verified": user.email_verified_at is not None,
        "is_staff": user.is_staff,
    }
