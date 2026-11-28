"""Calendar sync with Google and Microsoft: connect, busy time in, our events out, token refresh."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import select

from projectx.kernel.time import utcnow
from projectx.services import Services
from projectx.spine.calendar.models import CalendarBusy, CalendarConnection, CalendarEvent
from projectx.spine.calendar.sync import GOOGLE_API, GRAPH_API, save_connection
from projectx.spine.jobs.worker import Worker

IST = ZoneInfo("Asia/Kolkata")
HOURS = {d: [["09:00", "17:00"]] for d in ("mon", "tue", "wed", "thu", "fri")}


@pytest.fixture
def calendar_apps(services: Services, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services.settings, "google_client_id", "g-client")
    monkeypatch.setattr(services.settings, "google_client_secret", SecretStr("g-secret"))
    monkeypatch.setattr(services.settings, "microsoft_client_id", "m-client")
    monkeypatch.setattr(services.settings, "microsoft_client_secret", SecretStr("m-secret"))


def _monday(hour: int) -> datetime:
    today = utcnow().astimezone(IST).date()
    monday = today + timedelta(days=(7 - today.weekday()) or 7) + timedelta(days=7)
    return datetime.combine(monday, time(hour), tzinfo=IST)


async def _connect(services: Services, bid: uuid.UUID, provider: str, *, expires_in_s: int = 3600) -> uuid.UUID:
    creds = {
        "access_token": "old-token",
        "refresh_token": "refresh-1",
        "expires_at": (utcnow() + timedelta(seconds=expires_in_s)).isoformat(),
    }
    async with services.db.tenant(bid) as s:
        c = await save_connection(
            s, services, bid, provider=provider, label=f"founder@{provider}.test", credentials=creds, user_id=None
        )
        return c.id


async def test_connect_flow_stores_tokens_in_the_vault(
    services: Services, client: httpx.AsyncClient, founder: Any, business: dict[str, Any], calendar_apps: None
) -> None:
    base = f"/api/v1/b/{business['id']}"
    start = (await founder.get(f"{base}/calendar/connections/google/start")).json()
    url = httpx.URL(start["url"])
    assert url.host == "accounts.google.com" and url.params["code_challenge_method"] == "S256"
    assert "calendar.events" in url.params["scope"] and url.params["access_type"] == "offline"
    with respx.mock(assert_all_called=False) as mock:
        mock.post("https://oauth2.googleapis.com/token").mock(
            return_value=httpx.Response(200, json={"access_token": "a1", "refresh_token": "r1", "expires_in": 3600})
        )
        mock.get(f"{GOOGLE_API}/calendars/primary").mock(
            return_value=httpx.Response(200, json={"id": "founder@example.com"})
        )
        mock.get(f"{GOOGLE_API}/calendars/primary/events").mock(return_value=httpx.Response(200, json={"items": []}))
        res = await client.get(
            f"/oauth/calendar/google/callback?code=c&state={url.params['state']}", follow_redirects=False
        )
        assert res.status_code == 303 and "connected=google" in res.headers["location"]
        replay = await client.get(
            f"/oauth/calendar/google/callback?code=c&state={url.params['state']}", follow_redirects=False
        )
        assert "error=expired" in replay.headers["location"]  # a state works once
        await Worker(services).run_until_idle()
    items = (await founder.get(f"{base}/calendar/connections")).json()["items"]
    # Rare full-suite failure seen twice on 2026-11-28 and not reproduced since: report the state if it recurs.
    assert [(c["provider"], c["label"], c["status"]) for c in items] == [
        ("google", "founder@example.com", "connected")
    ], items
    assert items[0]["last_synced_at"] is not None, items
    assert "a1" not in json.dumps(items) and "r1" not in json.dumps(items)


async def test_google_busy_time_blocks_free_slots_and_events_go_out(
    services: Services, founder: Any, business: dict[str, Any], calendar_apps: None
) -> None:
    bid = uuid.UUID(business["id"])
    base = f"/api/v1/b/{bid}"
    await founder.put(f"{base}/calendar/settings", json={"working_hours": HOURS, "buffer_minutes": 0})
    day = _monday(0)
    busy = {
        "items": [
            {"id": "e1", "start": {"dateTime": _monday(10).isoformat()}, "end": {"dateTime": _monday(11).isoformat()}},
            {  # marked free: doesn't count
                "id": "e2",
                "transparency": "transparent",
                "start": {"dateTime": _monday(12).isoformat()},
                "end": {"dateTime": _monday(13).isoformat()},
            },
            {  # declined: doesn't count
                "id": "e3",
                "attendees": [{"self": True, "responseStatus": "declined"}],
                "start": {"dateTime": _monday(14).isoformat()},
                "end": {"dateTime": _monday(15).isoformat()},
            },
        ]
    }
    with respx.mock(assert_all_called=False) as mock:
        token = mock.post("https://oauth2.googleapis.com/token").mock(
            return_value=httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})
        )
        events = mock.get(f"{GOOGLE_API}/calendars/primary/events").mock(return_value=httpx.Response(200, json=busy))
        await _connect(services, bid, "google", expires_in_s=30)  # about to expire: refreshed before use
        await Worker(services).run_until_idle()
        assert token.called and events.calls.last.request.headers["authorization"] == "Bearer fresh"

        free = await founder.get(
            f"{base}/calendar/free",
            params={"start": day.isoformat(), "end": (day + timedelta(days=1)).isoformat(), "duration_minutes": 60},
        )
        starts = [
            datetime.fromisoformat(s["starts_at"]).astimezone(IST).strftime("%H:%M") for s in free.json()["slots"]
        ]
        assert "09:00" in starts and "09:30" not in starts and "10:00" not in starts and "10:30" not in starts
        assert "11:00" in starts and "12:00" in starts and "14:00" in starts  # free and declined time stay open

        clash = await founder.post(
            f"{base}/calendar/events", json={"title": "Clash", "starts_at": _monday(10).isoformat()}
        )
        assert clash.status_code == 409

        created = mock.post(f"{GOOGLE_API}/calendars/primary/events").mock(
            return_value=httpx.Response(200, json={"id": "g-ours"})
        )
        event = (
            await founder.post(f"{base}/calendar/events", json={"title": "Plan", "starts_at": _monday(16).isoformat()})
        ).json()
        await Worker(services).run_until_idle()
        sent = json.loads(created.calls.last.request.content)
        assert sent["summary"] == "Plan" and "attendees" not in sent  # the provider must never email anyone
        assert sent["extendedProperties"]["private"]["projectx_event"] == event["id"]

        patched = mock.patch(f"{GOOGLE_API}/calendars/primary/events/g-ours").mock(
            return_value=httpx.Response(200, json={"id": "g-ours"})
        )
        moved = await founder.post(
            f"{base}/calendar/events/{event['id']}/reschedule", json={"starts_at": _monday(15).isoformat()}
        )
        assert moved.status_code == 200, moved.text
        await Worker(services).run_until_idle()
        assert patched.called

        # Our own copy on Google is skipped when busy time comes back in, so it never blocks itself.
        busy["items"].append(
            {
                "id": "g-ours",
                "start": {"dateTime": _monday(15).isoformat()},
                "end": {"dateTime": _monday(16).isoformat()},
            }
        )
        async with services.db.tenant(bid) as s:
            connection_id = (await s.execute(select(CalendarConnection.id))).scalar_one()
        await founder.post(f"{base}/calendar/connections/{connection_id}/sync")
        await Worker(services).run_until_idle()
        async with services.db.tenant(bid) as s:
            blocks = (
                (await s.execute(select(CalendarBusy).where(CalendarBusy.connection_id == connection_id)))
                .scalars()
                .all()
            )
        assert len(blocks) == 1

        removed = mock.delete(f"{GOOGLE_API}/calendars/primary/events/g-ours").mock(return_value=httpx.Response(204))
        await founder.post(f"{base}/calendar/events/{event['id']}/cancel")
        await Worker(services).run_until_idle()
        assert removed.called
    async with services.db.tenant(bid) as s:
        row = await s.get(CalendarEvent, uuid.UUID(event["id"]))
    assert row is not None and row.external_refs == {}


async def test_microsoft_busy_and_revoked_access(
    services: Services, founder: Any, business: dict[str, Any], calendar_apps: None
) -> None:
    bid = uuid.UUID(business["id"])
    page2 = f"{GRAPH_API}/me/calendarView?page=2"
    with respx.mock(assert_all_called=False) as mock:
        view = mock.get(f"{GRAPH_API}/me/calendarView")
        view.side_effect = [
            httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "m1",
                            "showAs": "busy",
                            "start": {"dateTime": "2030-01-07T05:00:00.0000000", "timeZone": "UTC"},
                            "end": {"dateTime": "2030-01-07T06:00:00.0000000", "timeZone": "UTC"},
                        },
                        {
                            "id": "m2",
                            "showAs": "free",
                            "start": {"dateTime": "2030-01-07T07:00:00", "timeZone": "UTC"},
                            "end": {"dateTime": "2030-01-07T08:00:00", "timeZone": "UTC"},
                        },
                    ],
                    "@odata.nextLink": page2,
                },
            ),
            httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "m3",
                            "isCancelled": True,
                            "start": {"dateTime": "2030-01-08T05:00:00", "timeZone": "UTC"},
                            "end": {"dateTime": "2030-01-08T06:00:00", "timeZone": "UTC"},
                        }
                    ]
                },
            ),
        ]
        connection_id = await _connect(services, bid, "microsoft")
        await Worker(services).run_until_idle()
        assert view.calls[0].request.headers["prefer"] == 'outlook.timezone="UTC"'
    async with services.db.tenant(bid) as s:
        blocks = (await s.execute(select(CalendarBusy))).scalars().all()
    assert [(b.starts_at, b.ends_at) for b in blocks] == [
        (datetime(2030, 1, 7, 5, tzinfo=UTC), datetime(2030, 1, 7, 6, tzinfo=UTC))
    ]

    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{GRAPH_API}/me/calendarView").mock(return_value=httpx.Response(401))
        mock.post("https://login.microsoftonline.com/common/oauth2/v2.0/token").mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant"})
        )
        res = await founder.post(f"{base_url(bid)}/calendar/connections/{connection_id}/sync")
        assert res.status_code == 202
        await Worker(services).run_until_idle()
    async with services.db.tenant(bid) as s:
        c = await s.get(CalendarConnection, connection_id)
    assert c is not None and c.status == "error" and "reconnect" in (c.last_error or "")

    assert (await founder.delete(f"{base_url(bid)}/calendar/connections/{connection_id}")).status_code == 204
    async with services.db.tenant(bid) as s:
        assert (await s.execute(select(CalendarBusy))).scalars().all() == []


def base_url(bid: uuid.UUID) -> str:
    return f"/api/v1/b/{bid}"
