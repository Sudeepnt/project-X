"""Two-way calendar sync with Google Calendar and Microsoft Outlook (Graph).

In: every 10 minutes, busy time on each connected calendar (the next 60 days) is copied into
``calendar_busy`` as plain start/end pairs. Free-time search and double-booking checks then see the
founder's real day. Events this system put there itself are skipped, so moving one never clashes
with its own old copy. Free, cancelled and declined events don't count.

Out: events scheduled here are copied onto connected calendars (no attendees, so the provider never
emails anyone; invitations stay the job of the approved ``spine.calendar.send_invite`` action).
Reschedules update the copy and cancellations delete it.

Tokens live in the vault and are borrowed for one call at a time.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from projectx.kernel.errors import NotConfigured, ValidationFailed
from projectx.kernel.time import utcnow, zone
from projectx.services import Services
from projectx.settings import Settings
from projectx.spine.calendar.models import CalendarBusy, CalendarConnection, CalendarEvent
from projectx.spine.jobs.queue import enqueue
from projectx.spine.jobs.registry import JobContext, PermanentFailure, RetryLater, job, periodic
from projectx.spine.ledger.events import EventData, register_event
from projectx.spine.ledger.service import Actor, append
from projectx.spine.notifications.service import notify
from projectx.spine.tenancy.models import Business

PROVIDERS = ("google", "microsoft")
SYNC_DAYS = 60
GOOGLE_API = "https://www.googleapis.com/calendar/v3"
GRAPH_API = "https://graph.microsoft.com/v1.0"


class ConnectionChanged(EventData):
    connection_id: str
    provider: str
    change: str


register_event("spine.calendar.connection", "A calendar was connected, changed or removed", ConnectionChanged)


# ---------------------------------------------------------------- OAuth


@dataclass(frozen=True)
class ProviderConfig:
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    client_id: str
    client_secret: str
    extra: tuple[tuple[str, str], ...] = ()


def provider_config(provider: str, s: Settings) -> ProviderConfig:
    if provider == "google":
        cid, secret = s.google_client_id, s.google_client_secret
        if cid and secret:
            return ProviderConfig(
                "https://accounts.google.com/o/oauth2/v2/auth",
                "https://oauth2.googleapis.com/token",
                ("openid", "email", "https://www.googleapis.com/auth/calendar.events"),
                cid,
                secret.get_secret_value(),
                (("access_type", "offline"), ("prompt", "consent")),
            )
    elif provider == "microsoft":
        cid, secret = s.microsoft_client_id, s.microsoft_client_secret
        if cid and secret:
            return ProviderConfig(
                "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
                "https://login.microsoftonline.com/common/oauth2/v2.0/token",
                ("openid", "email", "offline_access", "User.Read", "Calendars.ReadWrite"),
                cid,
                secret.get_secret_value(),
            )
    else:
        raise ValidationFailed("Calendars connect from Google or Microsoft")
    label = "Google" if provider == "google" else "Microsoft"
    raise NotConfigured(f"The {label} calendar app isn't set up yet. Add its client id and secret to the environment.")


def redirect_uri(s: Settings, provider: str) -> str:
    return f"{s.base_url}/oauth/calendar/{provider}/callback"


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def authorization_url(provider: str, s: Settings, *, state: str, challenge: str) -> str:
    c = provider_config(provider, s)
    params = {
        "client_id": c.client_id,
        "redirect_uri": redirect_uri(s, provider),
        "response_type": "code",
        "scope": " ".join(c.scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        **dict(c.extra),
    }
    return f"{c.authorize_url}?{urlencode(params)}"


def _with_expiry(tokens: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    out = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token") or (previous or {}).get("refresh_token"),
        "expires_at": (utcnow() + timedelta(seconds=int(tokens.get("expires_in", 3600)))).isoformat(),
    }
    return out


async def exchange_code(
    provider: str, s: Settings, http: httpx.AsyncClient, *, code: str, verifier: str
) -> dict[str, Any]:
    c = provider_config(provider, s)
    res = await http.post(
        c.token_url,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(s, provider),
            "client_id": c.client_id,
            "client_secret": c.client_secret,
            "code_verifier": verifier,
        },
    )
    if res.status_code >= 400:
        raise ValidationFailed(f"The calendar refused the connection: {res.text[:200]}")
    return _with_expiry(res.json())


async def account_label(provider: str, http: httpx.AsyncClient, access_token: str) -> str:
    """The address of the connected account, so the owner can tell connections apart."""
    headers = {"Authorization": f"Bearer {access_token}"}
    if provider == "google":
        res = await http.get(f"{GOOGLE_API}/calendars/primary", headers=headers)
        return str(res.json().get("id", "")) if res.status_code < 400 else ""
    res = await http.get(f"{GRAPH_API}/me", params={"$select": "mail,userPrincipalName"}, headers=headers)
    if res.status_code >= 400:
        return ""
    body = res.json()
    return str(body.get("mail") or body.get("userPrincipalName") or "")


async def save_connection(
    session: AsyncSession,
    services: Services,
    business_id: uuid.UUID,
    *,
    provider: str,
    label: str,
    credentials: dict[str, Any],
    user_id: uuid.UUID | None,
) -> CalendarConnection:
    existing = await session.scalar(
        select(CalendarConnection).where(CalendarConnection.provider == provider, CalendarConnection.label == label)
    )
    if existing is not None:
        await services.vault.update(session, business_id, existing.secret_id, credentials)
        existing.status, existing.last_error = "connected", None
        connection, change = existing, "reconnected"
    else:
        secret_id = await services.vault.put(
            session, business_id, kind="oauth_token", label=f"calendar:{provider}", secret=credentials
        )
        connection = CalendarConnection(
            business_id=business_id, provider=provider, label=label[:200], secret_id=secret_id, connected_by=user_id
        )
        session.add(connection)
        await session.flush()
        change = "connected"
    await append(
        session,
        business_id=business_id,
        type="spine.calendar.connection",
        actor=Actor.user(user_id) if user_id else Actor.system("calendar"),
        subject=("calendar_connection", connection.id),
        data={"connection_id": str(connection.id), "provider": provider, "change": change},
    )
    await enqueue(
        session,
        "spine.calendar.sync",
        {"connection_id": str(connection.id)},
        business_id=business_id,
        idempotency_key=f"calsync:{connection.id}:{change}:{secrets.token_hex(6)}",
    )
    return connection


def connection_view(c: CalendarConnection) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "provider": c.provider,
        "label": c.label,
        "status": c.status,
        "push_events": c.push_events,
        "last_synced_at": c.last_synced_at.isoformat() if c.last_synced_at else None,
        "last_error": c.last_error,
    }


# ---------------------------------------------------------------- authorised calls


class CalendarClient:
    """One connection's API calls, refreshing the access token when it is about to expire."""

    def __init__(self, services: Services, business_id: uuid.UUID, connection_id: uuid.UUID, provider: str) -> None:
        self.services, self.business_id, self.connection_id, self.provider = (
            services,
            business_id,
            connection_id,
            provider,
        )
        self._token: str | None = None

    async def _credentials(self) -> tuple[uuid.UUID, dict[str, Any]]:
        async with self.services.db.tenant(self.business_id) as session:
            c = await session.get(CalendarConnection, self.connection_id)
            if c is None:
                raise PermanentFailure("calendar connection removed")
            creds = await self.services.vault.borrow(
                session, self.business_id, c.secret_id, purpose="calendar_sync", actor="calendar"
            )
            return c.secret_id, creds

    async def _refresh(self, secret_id: uuid.UUID, creds: dict[str, Any]) -> str:
        c = provider_config(self.provider, self.services.settings)
        if not creds.get("refresh_token"):
            raise PermanentFailure("access expired and there is no refresh token; reconnect the calendar")
        res = await self.services.http.post(
            c.token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": creds["refresh_token"],
                "client_id": c.client_id,
                "client_secret": c.client_secret,
            },
        )
        if res.status_code in (400, 401):
            raise PermanentFailure("the calendar provider revoked access; reconnect the calendar")
        if res.status_code >= 400:
            raise RetryLater(120, "token refresh failed")
        fresh = _with_expiry(res.json(), creds)
        async with self.services.db.tenant(self.business_id) as session:
            await self.services.vault.update(session, self.business_id, secret_id, fresh)
        return str(fresh["access_token"])

    async def token(self, *, force_refresh: bool = False) -> str:
        if self._token and not force_refresh:
            return self._token
        secret_id, creds = await self._credentials()
        expires = datetime.fromisoformat(creds["expires_at"]) if creds.get("expires_at") else None
        if force_refresh or expires is None or expires - utcnow() < timedelta(minutes=2):
            self._token = await self._refresh(secret_id, creds)
        else:
            self._token = str(creds["access_token"])
        return self._token

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        for attempt in (0, 1):
            headers["Authorization"] = f"Bearer {await self.token(force_refresh=attempt == 1)}"
            res = await self.services.http.request(method, url, headers=headers, **kwargs)
            if res.status_code != 401:
                break
        if res.status_code == 429 or res.status_code >= 500:
            raise RetryLater(float(res.headers.get("retry-after", 60)), f"{self.provider} {res.status_code}")
        if res.status_code == 401:
            raise PermanentFailure("the calendar provider refused the token; reconnect the calendar")
        return res


# ---------------------------------------------------------------- in: busy time


def _parse_google_time(node: dict[str, Any], tz_name: str) -> datetime:
    if "dateTime" in node:
        return datetime.fromisoformat(node["dateTime"].replace("Z", "+00:00")).astimezone(UTC)
    day = date.fromisoformat(node["date"])
    return datetime.combine(day, time(0), tzinfo=zone(node.get("timeZone") or tz_name)).astimezone(UTC)


async def google_busy(
    client: CalendarClient, start: datetime, end: datetime, tz_name: str, own: set[str]
) -> list[tuple[datetime, datetime]]:
    out: list[tuple[datetime, datetime]] = []
    params: dict[str, Any] = {
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": 2500,
        "fields": "nextPageToken,items(id,status,transparency,start,end,attendees(self,responseStatus))",
    }
    for _ in range(20):
        res = await client.request("GET", f"{GOOGLE_API}/calendars/primary/events", params=params)
        if res.status_code >= 400:
            raise PermanentFailure(f"google events: {res.status_code} {res.text[:200]}")
        body = res.json()
        for item in body.get("items", []):
            if item.get("id") in own or item.get("status") == "cancelled" or item.get("transparency") == "transparent":
                continue
            me = next((a for a in item.get("attendees", []) if a.get("self")), None)
            if me and me.get("responseStatus") == "declined":
                continue
            out.append((_parse_google_time(item["start"], tz_name), _parse_google_time(item["end"], tz_name)))
        if not body.get("nextPageToken"):
            break
        params["pageToken"] = body["nextPageToken"]
    return out


async def microsoft_busy(
    client: CalendarClient, start: datetime, end: datetime, own: set[str]
) -> list[tuple[datetime, datetime]]:
    out: list[tuple[datetime, datetime]] = []
    url: str | None = f"{GRAPH_API}/me/calendarView"
    params: dict[str, Any] | None = {
        "startDateTime": start.isoformat(),
        "endDateTime": end.isoformat(),
        "$select": "id,start,end,showAs,isCancelled,responseStatus",
        "$top": 500,
    }
    for _ in range(20):
        if url is None:
            break
        res = await client.request("GET", url, params=params, headers={"Prefer": 'outlook.timezone="UTC"'})
        if res.status_code >= 400:
            raise PermanentFailure(f"graph calendarView: {res.status_code} {res.text[:200]}")
        body = res.json()
        for item in body.get("value", []):
            if item.get("id") in own or item.get("isCancelled") or item.get("showAs") in ("free", "workingElsewhere"):
                continue
            if (item.get("responseStatus") or {}).get("response") == "declined":
                continue
            out.append((_graph_time(item["start"]), _graph_time(item["end"])))
        url, params = body.get("@odata.nextLink"), None
    return out


def _graph_time(node: dict[str, Any]) -> datetime:
    value = node["dateTime"].split(".")[0]
    moment = datetime.fromisoformat(value)
    tz = node.get("timeZone") or "UTC"
    return (moment.replace(tzinfo=zone(tz)) if moment.tzinfo is None else moment).astimezone(UTC)


@job("spine.calendar.sync", max_attempts=5, timeout_s=120)
async def sync_connection(ctx: JobContext) -> dict[str, Any]:
    business_id = ctx.require_business()
    connection_id = uuid.UUID(ctx.payload["connection_id"])
    services = ctx.services
    async with services.db.tenant(business_id) as session:
        c = await session.get(CalendarConnection, connection_id)
        business = await session.get(Business, business_id)
        if c is None or business is None:
            return {"skipped": "connection removed"}
        provider, tz_name = c.provider, business.timezone
        own = {
            str(ref)
            for refs in (await session.execute(select(CalendarEvent.external_refs))).scalars()
            for key, ref in (refs or {}).items()
            if key == str(connection_id)
        }
    start = utcnow() - timedelta(days=1)
    end = start + timedelta(days=SYNC_DAYS + 1)
    client = CalendarClient(services, business_id, connection_id, provider)
    try:
        busy = (
            await google_busy(client, start, end, tz_name, own)
            if provider == "google"
            else await microsoft_busy(client, start, end, own)
        )
    except PermanentFailure as exc:
        await _mark_error(services, business_id, connection_id, str(exc))
        raise
    async with services.db.tenant(business_id) as session:
        await session.execute(delete(CalendarBusy).where(CalendarBusy.connection_id == connection_id))
        for a, b in busy:
            if b > a:
                session.add(CalendarBusy(business_id=business_id, connection_id=connection_id, starts_at=a, ends_at=b))
        c = await session.get(CalendarConnection, connection_id)
        if c is not None:
            c.last_synced_at, c.status, c.last_error = utcnow(), "connected", None
            if c.connected_by is not None:
                # Busy time changed: have the assistant look for new clashes with what's booked here.
                from projectx.spine.context.proactive import wake_soon

                fingerprint = hashlib.sha256(repr(sorted(busy)).encode()).hexdigest()[:16]
                await wake_soon(
                    session,
                    business_id,
                    c.connected_by,
                    reason="Calendar changed",
                    key=f"sync:{connection_id}:{fingerprint}",
                )
    return {"busy_blocks": len(busy)}


async def _mark_error(services: Services, business_id: uuid.UUID, connection_id: uuid.UUID, error: str) -> None:
    async with services.db.tenant(business_id) as session:
        c = await session.get(CalendarConnection, connection_id)
        if c is None:
            return
        first_time = c.status != "error"
        c.status, c.last_error = "error", error[:500]
        if first_time:
            await notify(
                session,
                business_id,
                kind="calendar.error",
                level="warning",
                title=f"Your {'Google' if c.provider == 'google' else 'Outlook'} calendar stopped syncing",
                body=error[:300],
                link="/settings/calendar",
            )


@periodic("spine.calendar.schedule_syncs", every=timedelta(minutes=10))
async def schedule_syncs(services: Services, now: datetime) -> None:
    window = f"{now:%Y%m%d%H}{now.minute // 10}"
    async with services.db.session() as session:
        business_ids = list((await session.execute(select(Business.id).where(Business.status == "active"))).scalars())
    for business_id in business_ids:
        async with services.db.tenant(business_id) as session:
            ids = list(
                (
                    await session.execute(select(CalendarConnection.id).where(CalendarConnection.status == "connected"))
                ).scalars()
            )
            for connection_id in ids:
                await enqueue(
                    session,
                    "spine.calendar.sync",
                    {"connection_id": str(connection_id)},
                    business_id=business_id,
                    idempotency_key=f"calsync:{connection_id}:{window}",
                )


async def external_busy(session: AsyncSession, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    rows = await session.execute(
        select(CalendarBusy.starts_at, CalendarBusy.ends_at)
        .join(CalendarConnection, CalendarConnection.id == CalendarBusy.connection_id)
        .where(CalendarBusy.starts_at < end, CalendarBusy.ends_at > start)
    )
    return [(a, b) for a, b in rows.all()]


# ---------------------------------------------------------------- out: our events on their calendar


async def queue_push(session: AsyncSession, business_id: uuid.UUID, event: CalendarEvent) -> None:
    """After an event changes: copy the change to every connected calendar that wants it."""
    wanted = await session.scalar(
        select(func.count()).select_from(CalendarConnection).where(CalendarConnection.push_events.is_(True))
    )
    if wanted or event.external_refs:
        await enqueue(
            session,
            "spine.calendar.push_event",
            {"event_id": str(event.id)},
            business_id=business_id,
            idempotency_key=f"calpush:{event.id}:{event.sequence}:{event.status}",
        )


def _google_body(e: CalendarEvent) -> dict[str, Any]:
    return {
        "summary": e.title,
        "description": e.description,
        "location": e.location or "",
        "start": {"dateTime": e.starts_at.isoformat()},
        "end": {"dateTime": e.ends_at.isoformat()},
        "extendedProperties": {"private": {"projectx_event": str(e.id)}},
    }


def _graph_body(e: CalendarEvent) -> dict[str, Any]:
    fmt = "%Y-%m-%dT%H:%M:%S"
    return {
        "subject": e.title,
        "body": {"contentType": "text", "content": e.description},
        "location": {"displayName": e.location or ""},
        "start": {"dateTime": e.starts_at.astimezone(UTC).strftime(fmt), "timeZone": "UTC"},
        "end": {"dateTime": e.ends_at.astimezone(UTC).strftime(fmt), "timeZone": "UTC"},
        "transactionId": str(e.id),
    }


@job("spine.calendar.push_event", max_attempts=6, timeout_s=60)
async def push_event(ctx: JobContext) -> dict[str, Any]:
    business_id = ctx.require_business()
    services = ctx.services
    async with services.db.tenant(business_id) as session:
        event = await session.get(CalendarEvent, uuid.UUID(ctx.payload["event_id"]))
        if event is None:
            return {"skipped": "event removed"}
        connections = list(
            (
                await session.execute(select(CalendarConnection).where(CalendarConnection.status == "connected"))
            ).scalars()
        )
        session.expunge(event)
        targets = [(c.id, c.provider, c.push_events) for c in connections]
    refs: dict[str, Any] = dict(event.external_refs or {})
    done: list[str] = []
    for connection_id, provider, push in targets:
        key = str(connection_id)
        ref = refs.get(key)
        client = CalendarClient(services, business_id, connection_id, provider)
        base = f"{GOOGLE_API}/calendars/primary/events" if provider == "google" else f"{GRAPH_API}/me/events"
        if event.status == "cancelled" or not push:
            if ref:
                res = await client.request("DELETE", f"{base}/{ref}")
                if res.status_code < 400 or res.status_code in (404, 410):
                    refs.pop(key, None)
                    done.append(f"{provider}:deleted")
            continue
        body = _google_body(event) if provider == "google" else _graph_body(event)
        if ref:
            res = await client.request("PATCH", f"{base}/{ref}", json=body)
            if res.status_code in (404, 410):
                ref = None  # deleted on their side: put it back
        if not ref:
            res = await client.request("POST", base, json=body)
        if res.status_code >= 400:
            raise RetryLater(300, f"{provider} push {res.status_code}")
        refs[key] = res.json().get("id", ref)
        done.append(f"{provider}:saved")
    async with services.db.tenant(business_id) as session:
        fresh = await session.get(CalendarEvent, event.id, with_for_update=True)
        if fresh is not None:
            fresh.external_refs = refs
    return {"pushed": done}


async def disconnect(
    session: AsyncSession, services: Services, business_id: uuid.UUID, connection: CalendarConnection, *, actor: Actor
) -> None:
    await services.vault.delete(session, business_id, connection.secret_id)
    await append(
        session,
        business_id=business_id,
        type="spine.calendar.connection",
        actor=actor,
        subject=("calendar_connection", connection.id),
        data={"connection_id": str(connection.id), "provider": connection.provider, "change": "removed"},
    )
    await session.delete(connection)
