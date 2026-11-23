"""The job worker and the scheduler.

Workers claim jobs with ``FOR UPDATE SKIP LOCKED`` so any number of workers can run side by side
without double-processing. Each claimed job carries a lease that a heartbeat extends; if a
worker dies, the lease runs out and another worker takes the job. Only the lease holder may
finish a job (``WHERE locked_by = :me``), which fences out a worker that lost its lease.

The scheduler is leader-elected through a PostgreSQL advisory lock, so exactly one process
enqueues periodic work no matter how many workers run.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import random
import socket
import traceback
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import text

from projectx.kernel.time import utcnow
from projectx.spine.jobs.registry import (
    JobContext,
    PermanentFailure,
    RetryLater,
    get_job,
    periodic_specs,
)

if TYPE_CHECKING:
    from projectx.services import Services

log = structlog.get_logger("worker")

_CLAIM_SQL = text(
    """
    WITH next AS (
        SELECT id FROM jobs
        WHERE (status = 'queued' AND run_at <= now())
           OR (status = 'running' AND locked_until < now())
        ORDER BY priority, run_at
        LIMIT :n
        FOR UPDATE SKIP LOCKED
    )
    UPDATE jobs SET status = 'running', locked_by = :worker,
           locked_until = now() + make_interval(secs => :lease),
           attempts = jobs.attempts + 1, updated_at = now()
    FROM next WHERE jobs.id = next.id
    RETURNING jobs.id, jobs.kind, jobs.business_id, jobs.payload, jobs.attempts, jobs.max_attempts
    """
)
_HEARTBEAT_SQL = text(
    "UPDATE jobs SET locked_until = now() + make_interval(secs => :lease) "
    "WHERE id = :id AND locked_by = :worker AND status = 'running'"
)
_FINISH_SQL = text(
    "UPDATE jobs SET status = :status, result = CAST(:result AS jsonb), last_error = :error, "
    "finished_at = now(), locked_by = NULL, locked_until = NULL, updated_at = now() "
    "WHERE id = :id AND locked_by = :worker"
)
_REQUEUE_SQL = text(
    "UPDATE jobs SET status = 'queued', run_at = now() + make_interval(secs => :delay), "
    "attempts = jobs.attempts - :refund, last_error = :error, locked_by = NULL, "
    "locked_until = NULL, updated_at = now() WHERE id = :id AND locked_by = :worker"
)

SCHEDULER_LOCK_KEY = 7_012_345_678_901


def backoff_seconds(attempt: int) -> float:
    """Exponential backoff with full jitter: ~5s, 10s, 20s … capped at one hour."""
    ceiling = min(5 * (2 ** max(attempt - 1, 0)), 3600)
    return random.uniform(ceiling / 2, ceiling)  # noqa: S311 - jitter, not security


class Worker:
    def __init__(
        self,
        services: Services,
        *,
        worker_id: str | None = None,
        concurrency: int = 4,
        poll_interval: float = 1.0,
        lease_s: int = 120,
    ) -> None:
        self.services = services
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.concurrency = concurrency
        self.poll_interval = poll_interval
        self.lease_s = lease_s

    async def run(self, stop: asyncio.Event) -> None:
        log.info("worker.started", worker=self.worker_id, concurrency=self.concurrency)
        running: set[asyncio.Task[None]] = set()
        while not stop.is_set():
            free = self.concurrency - len(running)
            claimed: list[Any] = []
            if free > 0:
                try:
                    claimed = await self.claim(free)
                except Exception:
                    log.exception("worker.claim_failed")
            for row in claimed:
                task = asyncio.create_task(self.execute(row))
                running.add(task)
                task.add_done_callback(running.discard)
            if claimed and len(running) < self.concurrency:
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.poll_interval)
        if running:
            log.info("worker.draining", jobs=len(running))
            await asyncio.wait(running, timeout=self.lease_s)
        log.info("worker.stopped", worker=self.worker_id)

    async def run_until_idle(self, max_rounds: int = 1000) -> int:
        """Process jobs until none are ready. Used by tests and one-off maintenance commands."""
        processed = 0
        for _ in range(max_rounds):
            rows = await self.claim(self.concurrency)
            if not rows:
                break
            await asyncio.gather(*(self.execute(r) for r in rows))
            processed += len(rows)
        return processed

    async def claim(self, n: int) -> list[Any]:
        async with self.services.db.session() as session:
            result = await session.execute(_CLAIM_SQL, {"n": n, "worker": self.worker_id, "lease": self.lease_s})
            return list(result.mappings().all())

    async def execute(self, row: Any) -> None:
        job_id: uuid.UUID = row["id"]
        kind: str = row["kind"]
        bound = log.bind(
            job_id=str(job_id),
            kind=kind,
            attempt=row["attempts"],
            business_id=str(row["business_id"]) if row["business_id"] else None,
        )
        spec = get_job(kind)
        if spec is None:
            await self._finish(job_id, "dead", error=f"no handler registered for {kind!r}")
            return
        ctx = JobContext(
            services=self.services,
            job_id=job_id,
            kind=kind,
            attempt=row["attempts"],
            business_id=row["business_id"],
            payload=dict(row["payload"] or {}),
            log=bound,
        )
        heartbeat = asyncio.create_task(self._heartbeat(job_id))
        started = utcnow()
        try:
            result = await asyncio.wait_for(spec.handler(ctx), timeout=spec.timeout_s)
            await self._finish(job_id, "succeeded", result=result)
            bound.info("job.succeeded", ms=int((utcnow() - started).total_seconds() * 1000))
        except RetryLater as exc:
            await self._requeue(job_id, delay=exc.delay_s, error=exc.reason, refund=1)
            bound.info("job.deferred", delay_s=exc.delay_s, reason=exc.reason)
        except PermanentFailure as exc:
            await self._finish(job_id, "dead", error=str(exc))
            bound.warning("job.dead", error=str(exc))
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=8)}"
            if row["attempts"] >= row["max_attempts"]:
                await self._finish(job_id, "dead", error=error)
                bound.error("job.dead", error=str(exc))
            else:
                delay = backoff_seconds(row["attempts"])
                await self._requeue(job_id, delay=delay, error=error, refund=0)
                bound.warning("job.retrying", error=str(exc), delay_s=round(delay, 1))
        finally:
            heartbeat.cancel()

    async def _heartbeat(self, job_id: uuid.UUID) -> None:
        interval = max(self.lease_s / 3, 1)
        while True:
            await asyncio.sleep(interval)
            try:
                async with self.services.db.session() as session:
                    await session.execute(
                        _HEARTBEAT_SQL, {"id": job_id, "worker": self.worker_id, "lease": self.lease_s}
                    )
            except Exception:
                log.exception("worker.heartbeat_failed", job_id=str(job_id))

    async def _finish(
        self, job_id: uuid.UUID, status: str, *, result: dict[str, Any] | None = None, error: str | None = None
    ) -> None:
        from projectx.db.database import json_dumps

        async with self.services.db.session() as session:
            await session.execute(
                _FINISH_SQL,
                {
                    "id": job_id,
                    "worker": self.worker_id,
                    "status": status,
                    "result": json_dumps(result) if result is not None else None,
                    "error": error[:8000] if error else None,
                },
            )

    async def _requeue(self, job_id: uuid.UUID, *, delay: float, error: str, refund: int) -> None:
        async with self.services.db.session() as session:
            await session.execute(
                _REQUEUE_SQL,
                {
                    "id": job_id,
                    "worker": self.worker_id,
                    "delay": delay,
                    "error": error[:8000],
                    "refund": refund,
                },
            )


class Scheduler:
    def __init__(self, services: Services, *, tick_s: float = 15.0) -> None:
        self.services = services
        self.tick_s = tick_s
        self._last_run: dict[str, datetime] = {}

    async def run(self, stop: asyncio.Event) -> None:
        async with self.services.db.engine.connect() as conn:
            while not stop.is_set():
                got = bool(await conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": SCHEDULER_LOCK_KEY}))
                await conn.commit()
                if got:
                    break
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=30)
            if stop.is_set():
                return
            log.info("scheduler.leader")
            try:
                while not stop.is_set():
                    await self.tick(utcnow())
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=self.tick_s)
            finally:
                await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": SCHEDULER_LOCK_KEY})
                await conn.commit()

    async def tick(self, now: datetime) -> None:
        for spec in periodic_specs():
            last = self._last_run.get(spec.name)
            if last is not None and now - last < spec.every:
                continue
            try:
                await spec.fn(self.services, now)
            except Exception:
                log.exception("scheduler.periodic_failed", name=spec.name)
            self._last_run[spec.name] = now


__all__ = ["PermanentFailure", "RetryLater", "Scheduler", "Worker", "backoff_seconds"]
