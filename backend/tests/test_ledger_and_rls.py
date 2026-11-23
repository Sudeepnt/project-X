"""The ledger is permanent and tamper-evident; tenants are sealed off from each other in the database."""

from __future__ import annotations

import itertools
import uuid
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from projectx.db.registry import metadata
from projectx.db.rls import RLS_EXEMPT, tenant_tables
from projectx.services import Services
from projectx.spine.ledger.events import EventData, register_event
from projectx.spine.ledger.models import LedgerEvent
from projectx.spine.ledger.service import Actor, append, compute_hash, verify_chain
from projectx.spine.twin.models import Contact


class _Ping(EventData):
    n: int


register_event("test.ping", "test event", _Ping)


async def test_append_builds_a_verifiable_chain(services: Services, business: dict[str, Any]) -> None:
    bid = uuid.UUID(business["id"])
    async with services.db.tenant(bid) as s:
        for i in range(5):
            await append(s, business_id=bid, type="test.ping", actor=Actor.system(), data={"n": i})
    async with services.db.tenant(bid) as s:
        report = await verify_chain(s, bid)
        events = (
            (await s.execute(select(LedgerEvent).where(LedgerEvent.business_id == bid).order_by(LedgerEvent.seq)))
            .scalars()
            .all()
        )
    assert report.ok, report
    seqs = [e.seq for e in events]
    assert seqs == list(range(1, len(seqs) + 1))
    for prev, cur in itertools.pairwise(events):
        assert cur.prev_hash == prev.hash


async def test_idempotency_key_returns_the_existing_event(services: Services, business: dict[str, Any]) -> None:
    bid = uuid.UUID(business["id"])
    async with services.db.tenant(bid) as s:
        a = await append(
            s, business_id=bid, type="test.ping", actor=Actor.system(), data={"n": 1}, idempotency_key="k1"
        )
        b = await append(
            s, business_id=bid, type="test.ping", actor=Actor.system(), data={"n": 2}, idempotency_key="k1"
        )
    assert a.id == b.id


async def test_ledger_rejects_update_delete_and_truncate(services: Services, business: dict[str, Any]) -> None:
    bid = uuid.UUID(business["id"])
    for statement in (
        "UPDATE ledger_events SET type = 'x' WHERE business_id = :b",
        "DELETE FROM ledger_events WHERE business_id = :b",
        "TRUNCATE ledger_events",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            async with services.db.tenant(bid) as s:
                await s.execute(text(statement), {"b": bid})


async def test_tampering_is_detected_by_the_hash(services: Services, business: dict[str, Any]) -> None:
    bid = uuid.UUID(business["id"])
    async with services.db.tenant(bid) as s:
        event = await append(s, business_id=bid, type="test.ping", actor=Actor.system(), data={"n": 7})
    forged = compute_hash(
        business_id=bid,
        seq=event.seq,
        event_id=event.id,
        occurred_at=event.occurred_at,
        type=event.type,
        version=event.version,
        actor_type=event.actor_type,
        actor_id=event.actor_id,
        subject_type=None,
        subject_id=None,
        data={"n": 8},
        correlation_id=None,
        causation_id=None,
        prev_hash=event.prev_hash,
    )
    assert forged != event.hash


def test_event_schemas_cannot_hold_personal_data() -> None:
    class Leaky(EventData):
        email: str

    with pytest.raises(ValueError, match="personal-data"):
        register_event("test.leaky", "nope", Leaky)


def test_unknown_event_types_are_refused() -> None:
    from projectx.spine.ledger.events import get_event_type

    with pytest.raises(KeyError):
        get_event_type("test.never_declared")


async def test_every_tenant_table_has_forced_row_level_security(services: Services) -> None:
    async with services.db.session() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind = 'r'"
                )
            )
        ).all()
    state = {name: (rls, forced) for name, rls, forced in rows}
    for table in tenant_tables(metadata):
        assert state[table] == (True, True), f"{table} is not sealed by row-level security"
    for table in RLS_EXEMPT:
        if table in state:
            assert "business_id" in metadata.tables[table].columns


async def test_one_business_cannot_see_or_write_another(services: Services, founder: Any) -> None:
    from tests.conftest import new_business

    a = uuid.UUID((await new_business(founder))["id"])
    b = uuid.UUID((await new_business(founder))["id"])
    async with services.db.tenant(a) as s:
        s.add(Contact(business_id=a, email=f"x-{uuid.uuid4().hex[:6]}@example.com", stage="lead", status="new"))
    async with services.db.tenant(b) as s:
        visible = (await s.execute(select(Contact).where(Contact.business_id == a))).scalars().all()
        assert visible == []
    with pytest.raises(DBAPIError, match="row-level security"):
        async with services.db.tenant(b) as s:
            s.add(Contact(business_id=a, stage="lead", status="new"))
            await s.flush()
    async with services.db.session() as s:  # no tenant context at all: nothing is visible
        assert (await s.execute(select(Contact))).scalars().all() == []
