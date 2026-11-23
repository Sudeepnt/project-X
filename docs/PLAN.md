# Project X — build plan and status

Project X is an operating system that runs a whole company with AI agents. This repository holds
the **spine** (the parts every department shares) and the **modules** (one per department).
The first module is **GTM** (go-to-market): brand, goals, campaigns, content, publishing, CRM,
nurture, analytics and learning. Its first customer is Bought (a media business).

This file is the source of truth for what is built and what is next. Update it with every milestone.

## Non-negotiable rules (from the master blueprint)

1. **Closed loops only.** Every pack starts on a trigger, ends in a measured outcome, writes that
   outcome to the ledger, and changes how it runs next time.
2. **The policy engine is plain code.** Every agent action is sorted into
   `NEVER < DRAFT < ASK_FIRST < RUN_AND_TELL < RUN` before it runs. One-way doors (money out,
   contracts, filings, public statements that are new in kind) never go above `ASK_FIRST`.
3. **Agents never hold keys.** Credentials live encrypted in the vault; adapters borrow them for
   one call, and every borrow is logged.
4. **Numbers come from code.** A model may explain a number; it never produces one. Reports are
   computed first and narrated second, with figures filled in by code.
5. **One business, one sealed room.** Every tenant table carries `business_id` and is protected by
   PostgreSQL row-level security, not only by application filters.
6. **No PII in the ledger.** The ledger is append-only and permanent, so it stores ids, never
   names or emails. Erasure requests delete PII from mutable tables; the ledger stays intact.
7. **Cost first.** Every model call goes through the gateway: cheapest rung first, verifier
   checks, escalate only on failure, every call logged with its cost, daily cap per business.
8. **Verifiers read outcomes from records**, never from what the agent says it did.

## Architecture in one picture

```
web/ (React cockpit)  ──HTTPS──>  backend/ FastAPI (api)  ──>  PostgreSQL 18 + pgvector
                                   backend/ worker (jobs, schedules, daily cycle)
                                   backend/ discord bot (community pack)

backend/src/projectx/
  kernel/    ids, time, money, errors, pagination          (no business logic)
  db/        engine, sessions, tenancy (RLS), base models
  spine/     identity, tenancy, ledger, jobs, policy, decisions, vault, gateway,
             blobs, notifications, billing, payments, cockpit, packs, memory
  modules/
    gtm/     brand, goals, campaigns, content, channels, crm, links, landing,
             email, inbox, analytics, experiments, ads, community, compliance, sales
```

A module plugs into the spine through one `Module` object that registers its action types,
ledger event types, API routers, job handlers, schedules, packs and cockpit contributions.
Future modules (finance, legal, people…) plug in the same way.

## Milestones

- [ ] M0 Foundation: repo, tooling, CI, local Postgres 18, app skeleton, migrations
- [ ] M1 Spine: identity + tenancy (RLS), ledger, jobs, policy + autonomy + decisions,
      vault, model gateway (Anthropic / OpenAI-compatible / offline), notifications, cockpit
- [ ] M2 GTM core: brand book + ICP, CRM (stages, statuses, automations, deals),
      tracked links + attribution, landing pages + A/B, goals (diagnosis, feasibility, pacing,
      stop rules), campaign library + selection, content engine (themes, hooks, ratio,
      calendar, generation, checks, repurposing), publishing (adapters, OAuth, queue, limits,
      verification, simulator), inbox, email + sequences + newsletter, analytics + weekly
      report, experiments + learning memory, compliance, media kit
- [ ] M3 Web cockpit: design system, shell, auth, onboarding, every module screen
- [ ] M4 Phase 2/3: Discord bot, ads engine (Meta/Google) with caps, billing + plans + usage,
      unified social API mode, cross-business benchmarks (opt-in)
- [ ] M5 Hardening: security headers/CSP, rate limits, performance, docs, deploy guide

## Status log

(Newest first.)
