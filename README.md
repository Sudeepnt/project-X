# Project X

**An operating system that runs a whole company.** Every department is a module run by AI agents,
every module closes its own loop, and the founder steers from one cockpit: a morning briefing, a
short queue of decisions, and three numbers (revenue, profit, churn).

This repository holds the **spine** that every department shares, and the **modules**, one per
department. The first module is **GTM** (go-to-market).

> Status and roadmap: [`docs/PLAN.md`](docs/PLAN.md) · Architecture: [`docs/architecture.md`](docs/architecture.md) · Deploying: [`docs/DEPLOY.md`](docs/DEPLOY.md)

## What makes it trustworthy

| Rule | How the code enforces it |
|---|---|
| A person owns every decision that can't be undone | The **policy engine** is plain code, not AI. Every agent action is sorted into `Never < Draft < Ask first < Run and tell < Run`. One-way doors (money out, contracts, filings, public statements new in kind) can never run without approval. Unregistered actions are blocked. |
| Autonomy is earned slowly and lost instantly | Clean streaks (no edits, no rejections, nothing undone) earn a promotion offer the founder must accept; incidents and changed conditions drop a level at once. |
| Everything is on the record | An **append-only, hash-chained ledger**. A database trigger rejects UPDATE/DELETE/TRUNCATE; any tampering breaks the chain and is detected. No personal data goes into it, so erasure requests stay possible. |
| One business, one sealed room | PostgreSQL **row-level security**, enabled *and forced* on every tenant table, set per transaction. A test fails if a new tenant table forgets it. |
| Agents never hold keys | Credentials are sealed with AES-256-GCM under a per-business key (envelope encryption), bound to their row, and every borrow is logged with its purpose. |
| Numbers come from code | Models may explain a figure, never produce one. Reports are computed first, narrated second. |
| Cost first | A **model gateway** routes every call to the cheapest rung that passes its check, climbs only on failure, caches answers, logs every call's cost and enforces a daily cap per business. It runs fully **offline** with no API key. |

## Run it locally

Requirements: macOS or Linux, [uv](https://docs.astral.sh/uv/), Node 22+, [pnpm](https://pnpm.io/),
PostgreSQL 18 with pgvector (`brew install postgresql@18 pgvector uv pnpm`).

```bash
make setup     # start Postgres, create the app role and databases, migrate, install web deps
make api       # API on http://localhost:8010  (docs at /api/docs)
make worker    # background jobs and the daily cycle (separate terminal)
make web-dev   # cockpit with hot reload on http://localhost:5173 (separate terminal)
make seed      # optional: a practice company with sample data (prints the demo sign-in)
```

Or run the whole production-like stack in containers:

```bash
docker compose up --build     # http://localhost:8010 (API + cockpit) · mail catcher http://localhost:8025
```

Everything works without any external account: model calls use the offline provider, and
platforms run in **practice mode** against a simulator with injected failures. Add keys in `.env`
(see [`.env.example`](.env.example)) when you are ready.

## Checks

```bash
make check     # lint, strict type check, backend tests, web lint/types/tests
```

Backend tests run against a real PostgreSQL as a non-superuser role (superusers bypass row-level
security), rebuilding the schema with Alembic each session.

## Layout

```
backend/   Python 3.13 · FastAPI · SQLAlchemy 2 (async) · PostgreSQL 18 · Alembic
  src/projectx/kernel    ids, time, money, errors, pagination
  src/projectx/db        engine, tenant sessions, row-level security
  src/projectx/spine     identity, tenancy, ledger, jobs, policy, decisions, vault,
                         gateway, memory, notifications, billing, twin, cockpit
  src/projectx/modules   department modules (gtm/ first)
web/       React · TypeScript · Vite · TanStack Router/Query · Tailwind — the cockpit
infra/     local scripts, database bootstrap for containers
Dockerfile, docker-compose.yml   one image for API, cockpit and worker
docs/      plan, architecture, decisions, module specs
```
