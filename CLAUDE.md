# Project X: instructions for any agent working in this repo

Read `docs/PLAN.md` first (milestones, status log, rules). Production-grade, Apple-level quality is the bar.

## Loop for each run
1. `git pull`, read the top of the status log and the unchecked milestones in `docs/PLAN.md`.
2. Pick the next unfinished item (order: M4 ads engine, billing (Dodo), Discord bot, asset library, WhatsApp,
   cross-business benchmarks; then M5 Docker images, compose, deploy docs, architecture docs).
3. Build it end to end with tests (backend) and a screen (web) where it has UI.
4. Run the checks below. Fix failures. Never commit red.
5. Update `docs/PLAN.md` status log (newest first), commit, push to `main`. One focused commit per item.

## Checks
- Backend (`backend/`): `uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src && uv run pytest -q`
  Needs Postgres 18 + pgvector and the non-superuser role (see `infra/scripts/dev-db.sh`, `.github/workflows/ci.yml`).
  If Postgres is unavailable in your environment, still run ruff + mypy and rely on CI for tests; check CI results with `gh run list`.
- Web (`web/`): `pnpm install && pnpm run check && pnpm run build` (pnpm's release-age policy is on: don't add exceptions).

## Hard rules
- Tenant tables get RLS (migration + `db/rls.py`); no PII in ledger events; registered event types only.
- Money-moving or public actions go through the policy engine and decisions (never act directly).
- No secrets in the repo. Dev API port is 8010. Don't touch other projects on the machine.
- Don't recreate existing games or products; names/brands are placeholders ("Project X").
