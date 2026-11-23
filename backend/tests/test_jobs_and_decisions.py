"""Background jobs, the decision pipeline, autonomy and the vault."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select, text

from projectx.services import Services
from projectx.spine.decisions.executors import ExecutionContext, ExecutionResult, executor, undoer
from projectx.spine.decisions.models import Decision
from projectx.spine.decisions.service import Proposal, propose
from projectx.spine.jobs.models import Job
from projectx.spine.jobs.queue import enqueue
from projectx.spine.jobs.registry import JobContext, RetryLater, job
from projectx.spine.jobs.worker import Worker
from projectx.spine.ledger.models import LedgerEvent
from projectx.spine.memory.models import MemoryCase
from projectx.spine.policy.actions import ActionType, register_action
from projectx.spine.policy.levels import Level
from projectx.spine.policy.models import AutonomyState, PolicySettings

CALLS: dict[str, int] = {"flaky": 0, "deferred": 0}
EXECUTED: list[dict[str, Any]] = []

register_action(
    ActionType(
        key="test.publish",
        module="test",
        title="Publish a test post",
        description="",
        default_level=Level.ASK_FIRST,
        max_level=Level.RUN,
        reversible=True,
        public=True,
        undo="Delete the post",
    )
)
register_action(
    ActionType(
        key="test.tidy",
        module="test",
        title="Tidy",
        description="",
        default_level=Level.RUN,
        max_level=Level.RUN,
        reversible=True,
        undo="Untidy",
    )
)


@executor("test.publish")
async def _publish(ctx: ExecutionContext) -> ExecutionResult:
    EXECUTED.append(ctx.payload)
    return ExecutionResult(data={"post": "ok"}, undo_payload={"post_id": "p1"}, external_ref="p1")


@undoer("test.publish")
async def _unpublish(ctx: ExecutionContext) -> dict[str, Any]:
    return {"deleted": ctx.undo_payload}


@executor("test.tidy")
async def _tidy(ctx: ExecutionContext) -> ExecutionResult:
    return ExecutionResult(data={"tidied": True})


@job("test.flaky", max_attempts=3)
async def _flaky(ctx: JobContext) -> dict[str, Any] | None:
    CALLS["flaky"] += 1
    raise RuntimeError("boom")


@job("test.deferred")
async def _deferred(ctx: JobContext) -> dict[str, Any] | None:
    CALLS["deferred"] += 1
    raise RetryLater(3600, "rate limited")


async def _job(services: Services, job_id: uuid.UUID) -> Job:
    async with services.db.session() as s:
        row = await s.get(Job, job_id)
        assert row is not None
        return row


async def test_failed_jobs_retry_with_backoff_then_die(services: Services) -> None:
    async with services.db.session() as s:
        job_id = await enqueue(s, "test.flaky")
    assert job_id is not None
    worker = Worker(services)
    for _ in range(3):
        await worker.run_until_idle()
        async with services.db.session() as s:  # make the retry due now
            await s.execute(text("UPDATE jobs SET run_at = now() WHERE id = :id AND status = 'queued'"), {"id": job_id})
    row = await _job(services, job_id)
    assert row.status == "dead"
    assert row.attempts == 3
    assert "boom" in (row.last_error or "")


async def test_retry_later_does_not_burn_attempts(services: Services) -> None:
    async with services.db.session() as s:
        job_id = await enqueue(s, "test.deferred")
    assert job_id is not None
    await Worker(services).run_until_idle()
    row = await _job(services, job_id)
    assert row.status == "queued"
    assert row.attempts == 0


async def test_idempotency_keys_dedupe_jobs(services: Services) -> None:
    key = f"once:{uuid.uuid4()}"
    async with services.db.session() as s:
        first = await enqueue(s, "test.deferred", idempotency_key=key)
        second = await enqueue(s, "test.deferred", idempotency_key=key)
    assert first is not None and second is None


async def test_expired_lease_is_reclaimed(services: Services) -> None:
    async with services.db.session() as s:
        job_id = await enqueue(s, "spine.send_email", {"to": "a@example.com", "subject": "s", "html": "h", "text": "t"})
        await s.execute(
            text(
                "UPDATE jobs SET status='running', locked_by='dead-worker', "
                "locked_until = now() - interval '1 minute' WHERE id = :id"
            ),
            {"id": job_id},
        )
    await Worker(services).run_until_idle()
    row = await _job(services, job_id)  # type: ignore[arg-type]
    assert row.status == "succeeded"
    assert row.payload == {}  # address dropped after delivery


async def _pending(services: Services, bid: uuid.UUID, decision_id: uuid.UUID) -> Decision:
    async with services.db.tenant(bid) as s:
        d = await s.get(Decision, decision_id)
        assert d is not None
        return d


async def test_ask_first_waits_then_runs_after_approval(
    services: Services, founder: Any, business: dict[str, Any]
) -> None:
    bid = uuid.UUID(business["id"])
    async with services.db.tenant(bid) as s:
        outcome = await propose(
            s,
            bid,
            Proposal(
                action_type="test.publish",
                title="Post about launch",
                payload={"text": "hello"},
                requested_by="pack:test",
            ),
        )
    assert outcome.status == "waiting_for_approval"
    assert outcome.decision_id is not None
    res = await founder.post(f"/api/v1/b/{bid}/decisions/{outcome.decision_id}/approve", json={})
    assert res.status_code == 200, res.text
    await Worker(services).run_until_idle()
    d = await _pending(services, bid, outcome.decision_id)
    assert d.status == "executed"
    async with services.db.tenant(bid) as s:
        types = (await s.execute(select(LedgerEvent.type).where(LedgerEvent.business_id == bid))).scalars().all()
        state = await s.get(AutonomyState, (bid, "test.publish"))
    assert {"spine.action.proposed", "spine.decision.approved", "spine.action.executed"} <= set(types)
    assert state is not None and state.clean_streak == 1

    undo = await founder.post(f"/api/v1/b/{bid}/decisions/{outcome.decision_id}/undo", json={})
    assert undo.status_code == 200
    await Worker(services).run_until_idle()
    d = await _pending(services, bid, outcome.decision_id)
    assert d.status == "undone"
    async with services.db.tenant(bid) as s:
        state = await s.get(AutonomyState, (bid, "test.publish"))
    assert state is not None and state.clean_streak == 0


async def test_rejection_is_remembered(services: Services, founder: Any, business: dict[str, Any]) -> None:
    bid = uuid.UUID(business["id"])
    async with services.db.tenant(bid) as s:
        outcome = await propose(s, bid, Proposal(action_type="test.publish", title="Salesy post"))
    res = await founder.post(
        f"/api/v1/b/{bid}/decisions/{outcome.decision_id}/reject",
        json={"reason_code": "too_salesy", "note": "Lead with the story"},
    )
    assert res.status_code == 200
    async with services.db.tenant(bid) as s:
        cases = (await s.execute(select(MemoryCase).where(MemoryCase.business_id == bid))).scalars().all()
    assert any("Too salesy" in c.lesson for c in cases)


async def test_run_level_executes_immediately(services: Services, business: dict[str, Any]) -> None:
    bid = uuid.UUID(business["id"])
    async with services.db.tenant(bid) as s:
        outcome = await propose(s, bid, Proposal(action_type="test.tidy", title="Tidy tags"))
    assert outcome.status == "running"
    await Worker(services).run_until_idle()
    assert (await _pending(services, bid, outcome.decision_id)).status == "executed"  # type: ignore[arg-type]


async def test_unregistered_action_is_blocked(services: Services, business: dict[str, Any]) -> None:
    bid = uuid.UUID(business["id"])
    async with services.db.tenant(bid) as s:
        outcome = await propose(s, bid, Proposal(action_type="test.wire_money_overseas", title="nope"))
    assert outcome.status == "blocked"
    assert outcome.decision_id is None


async def test_clean_streak_earns_a_promotion_offer(services: Services, founder: Any, business: dict[str, Any]) -> None:
    bid = uuid.UUID(business["id"])
    async with services.db.tenant(bid) as s:
        policy = await s.get(PolicySettings, bid)
        assert policy is not None
        policy.promotion_threshold = 2
        policy.approval_mode = "auto"
    for i in range(2):
        async with services.db.tenant(bid) as s:
            outcome = await propose(s, bid, Proposal(action_type="test.publish", title=f"Post {i}"))
        await founder.post(f"/api/v1/b/{bid}/decisions/{outcome.decision_id}/approve", json={})
        await Worker(services).run_until_idle()
    async with services.db.tenant(bid) as s:
        offer = await s.scalar(
            select(Decision).where(
                Decision.business_id == bid, Decision.kind == "promotion", Decision.status == "pending"
            )
        )
    assert offer is not None
    res = await founder.post(f"/api/v1/b/{bid}/decisions/{offer.id}/approve", json={})
    assert res.status_code == 200
    async with services.db.tenant(bid) as s:
        state = await s.get(AutonomyState, (bid, "test.publish"))
    assert state is not None and state.level == int(Level.RUN_AND_TELL)


async def test_vault_round_trip_binding_and_rotation(services: Services, business: dict[str, Any]) -> None:
    from projectx.spine.vault.models import VaultAccess, VaultSecret
    from projectx.spine.vault.service import VaultError

    bid = uuid.UUID(business["id"])
    async with services.db.tenant(bid) as s:
        a = await services.vault.put(s, bid, kind="oauth_token", label="x", secret={"access_token": "A"})
        b = await services.vault.put(s, bid, kind="oauth_token", label="y", secret={"access_token": "B"})
    async with services.db.tenant(bid) as s:
        assert await services.vault.borrow(s, bid, a, purpose="publish", actor="test") == {"access_token": "A"}
        logs = (await s.execute(select(VaultAccess).where(VaultAccess.secret_id == a))).scalars().all()
        assert len(logs) == 1
        # Copy B's ciphertext into A's row: authenticated encryption must refuse it.
        row_a, row_b = await s.get(VaultSecret, a), await s.get(VaultSecret, b)
        assert row_a and row_b
        row_a.ciphertext, row_a.nonce = row_b.ciphertext, row_b.nonce
        await s.flush()
        with pytest.raises(VaultError):
            await services.vault.borrow(s, bid, a, purpose="publish", actor="test")
        await s.rollback()
    async with services.db.tenant(bid) as s:
        version = await services.vault.rotate(s, bid)
        assert version == 2
    async with services.db.tenant(bid) as s:
        assert await services.vault.borrow(s, bid, b, purpose="publish", actor="test") == {"access_token": "B"}
