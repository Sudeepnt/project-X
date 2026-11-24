# Handoff: continuing Project X on another machine

Written 2026-11-24. The repository is the single source of truth. Nothing needed to continue lives only on the
original Mac, except the items under "Not in the repo" below.

## Where everything is

- **GitHub:** https://github.com/Sudeepnt/project-X (public), branch `main`
- **Original machine:** `/Users/sudeepnt/project-X`
- `main` was green on CI at commit `f69574d` (backend, web, and Docker image jobs).

## What exists (all built, tested, pushed)

Milestones M0 to M5 in `docs/PLAN.md` are done. Read its status log, newest first, for the full story.

**Spine** (`backend/src/projectx/spine`): identity and sessions, tenancy with PostgreSQL row-level security,
append-only hash-chained ledger, policy engine plus autonomy plus decisions, jobs and scheduler, vault
(envelope encryption), model gateway (offline, Anthropic, OpenAI-compatible), notifications, billing (Dodo
Payments), memory.

**GTM module** (`backend/src/projectx/modules/gtm`): brand book, goals with Monte Carlo forecasts, 33-playbook
campaign library, content pipeline (plan, write, adapt, check, approve), 14 platform adapters plus a practice
simulator, CRM and funnel, email sequences and newsletter, landing pages with A/B, shared inbox, analytics and
weekly reports, compliance (export, erasure, Meta data-deletion), media kit, ads engine (Meta, Google), Discord
bot, WhatsApp, asset library, repurposing, budget planner, A/B lab for posts, opt-in benchmarks.

**Cockpit** (`web/`): React + TypeScript + Tailwind v4, light and dark, hand-rolled SVG charts. 18 screens, zero
axe-core WCAG 2.2 AA violations in both themes.

**Deploy:** `Dockerfile`, `docker-compose.yml`, `docs/DEPLOY.md`, `docs/architecture.md`.

Tests: 114 backend, 12 web. Migrations: `0001` to `0005`.

## Run it on the new machine

Easiest, if Docker is installed:

```bash
git clone https://github.com/Sudeepnt/project-X.git && cd project-X
docker compose up --build              # http://localhost:8010 (API + cockpit), mail catcher on :8025
docker compose run --rm api projectx seed-demo   # prints a demo login; sign in with it
```

Native development (macOS or Linux; on Windows use WSL2): needs `uv`, Node 22+, `pnpm@12`, and PostgreSQL 18 with
pgvector.

```bash
make setup      # database, migrations, web dependencies (see infra/scripts/dev-db.sh)
make api        # http://localhost:8010
make worker     # second terminal
make web-dev    # third terminal: cockpit with hot reload on :5173
make check      # lint, strict types, all tests, web checks
```

Port 8010 is used on purpose; port 8000 belonged to an unrelated project on the original Mac.

## Rules that must keep holding (also in `CLAUDE.md`)

- Every tenant table gets row-level security (migration plus `db/rls.py`). A test fails if one forgets.
- No personal data in ledger events; event types are registered with schemas.
- Anything public, costly or irreversible goes through the policy engine and decisions, never directly.
- Never commit red: ruff, `ruff format --check`, mypy strict, pytest, and `pnpm run check && pnpm run build`.
- pnpm's minimum-release-age policy is on. Don't add exceptions; lower the version range instead.
- Commit identity is `Sudeepnt <98895899+Sudeepnt@users.noreply.github.com>` (repo-local) to keep the real email private.

## Not in the repo (and why)

- `.env` and any real keys: gitignored on purpose. Copy `.env.example` and fill in only what you need.
- Local database contents and the demo password: recreate with `make setup` and `projectx seed-demo`.
- Claude Code's per-machine memory notes (`~/.claude/projects/.../memory`): not synced. This file and
  `docs/PLAN.md` replace them.

## Known gaps and good next steps

1. **Real platform connections.** Needs app keys and reviews (Meta, TikTok, LinkedIn). `docs/DEPLOY.md` has the
   exact variables and callback URLs. Until then everything runs against the simulator.
2. **Dodo products.** Create the three plans in Dodo and set `PX_DODO_PRODUCT_IDS`.
3. **Docker was never run locally** on the original Mac (not installed there). CI builds and smoke-tests the image;
   running `docker compose up` on the new machine is the first real local check.
4. **Legal pages** are generated; a lawyer should review them.
5. **Older approval cards** created before the time-zone fix still say "UTC"; new ones use the business time zone.
6. **Ideas not built yet:** more department modules beyond GTM (finance, support), a native mobile layout pass,
   end-to-end browser tests in CI (today's browser checks were run by hand with puppeteer), and a status page.
7. **Cloud routine:** an overnight cloud routine was created and then disabled (`trig_01AEAbB6XS76Ep8NgKTeJG5e`).
   It can only be deleted at claude.ai/code/routines. The owner prefers running work locally, not in the cloud.

## Prompt to paste into Claude Code on the new machine

```text
Open the repository at https://github.com/Sudeepnt/project-X (clone it if it isn't on this machine yet, then work
inside it). Read CLAUDE.md, docs/HANDOFF.md and the top of docs/PLAN.md before doing anything.

Context: Project X is an AI-agent OS that runs a company; GTM (go-to-market) is the first module and it is fully
built, tested and green on CI (milestones M0 to M5). The standard is production grade, Apple-level quality: many
people will use this.

First, get it running here: follow "Run it on the new machine" in docs/HANDOFF.md, run `make check`, sign in to the
cockpit with the demo company, and tell me what works and what doesn't. Don't change code until that is done.

Then ask me which of the "Known gaps and good next steps" to take first. Work in small focused commits, run the
full checks before each one, update docs/PLAN.md's status log, and push to main. Keep secrets out of the repo.
```
