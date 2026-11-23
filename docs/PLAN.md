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

- [x] M0 Foundation: repo, tooling, CI, local Postgres 18, app skeleton, migrations
- [x] M1 Spine: identity + tenancy (RLS), ledger, jobs, policy + autonomy + decisions,
      vault, model gateway (Anthropic / OpenAI-compatible / offline), notifications, cockpit
- [x] M2 GTM core: brand book + ICP, CRM (stages, statuses, automations, deals),
      tracked links + attribution, landing pages + A/B, goals (diagnosis, feasibility, pacing,
      stop rules), campaign library + selection, content engine (themes, hooks, ratio,
      calendar, generation, checks, repurposing), publishing (adapters, OAuth, queue, limits,
      verification, simulator), inbox, email + sequences + newsletter, analytics + weekly
      report, experiments + learning memory, compliance, media kit
- [x] M3 Web cockpit: design system, shell, auth, onboarding, every module screen
- [x] M4 Phase 2/3: Discord bot, ads engine (Meta/Google) with caps, billing + plans + usage,
      unified social API mode, cross-business benchmarks (opt-in)
- [ ] M5 Hardening: security headers/CSP, rate limits, performance, docs, deploy guide

## Status log

(Newest first.)

- 2026-11-23 — M5 deploy and hardening, part 1: one Docker image (non-root, health check, JSON logs) serving the
  API, cockpit and worker; docker-compose stack (Postgres + pgvector with a non-superuser role, migrate job,
  mail catcher); CI builds and smoke-tests the image. S3-compatible storage (encrypted, presigned URLs; AWS S3,
  R2, MinIO) next to local disk. `projectx generate-keys`. Production refuses plain-http web URLs, a dev mail
  host or S3 without a bucket. Security fix: the API trusted X-Forwarded-For from anyone (rate limits could be
  dodged); now only configured proxies. docs/DEPLOY.md and docs/architecture.md. 109 backend tests.
  Not yet verified locally: Docker isn't installed on the dev Mac; the CI image job is the check.

- 2026-11-23 — Cross-business benchmarks (M4 complete): opt-in only, and only contributors can see them.
  Weekly rates (engagement, reach per post, posts per week, page conversion, USD cost per lead) are stored
  under a salted one-way contributor id with no business id (migration 0005). A figure shows only when at
  least 5 similar businesses contribute; turning sharing off deletes everything shared. Analytics →
  Benchmarks shows you against the middle half and the median. 107 backend + 12 web tests.

- 2026-11-23 — WhatsApp (M4) via the Cloud API: connect with token + phone number ID (verified against the
  number's profile); inbound messages arrive through the signed Meta webhook into the inbox and the CRM by
  phone; START records opt-in with evidence, STOP withdraws it; delivery/read/failure receipts update
  messages; sequences can send by WhatsApp (free text inside the 24-hour window, approved templates outside
  it, otherwise skipped with the reason; migration 0004 adds message metadata); inbox replies over WhatsApp.
  Fix (all platforms): connecting an account already connected to another business silently left its
  webhooks with the first business; now refused while it's live there, and taken over once it's removed.
  CI confirmed green on GitHub. 104 backend tests.

- 2026-11-23 — Asset library (M4): uploads typed by their bytes (never the browser's claim), per-type size
  limits, image dimensions, EXIF/IPTC stripped from JPEGs (no GPS or device data leaks), tags, alt text,
  search, signed URLs, deletion blocked while an upcoming post uses the file. Content → Library view and
  "Add media" on each platform version. Security fix: post media could reference any storage key, including
  another business's files; now only this business's library files or https links. 99 backend tests.

- 2026-11-23 — Discord community bot (M4) over HTTP interactions (Ed25519-verified, 5-minute replay
  window; no gateway process). Install from the cockpit; the OAuth code exchange proves the server.
  /ask lands in the shared inbox and follows the same reply policy as every channel (common questions
  answered in the brand voice, the rest to a person), answered in-channel with a scoped mention;
  /subscribe records consent evidence and uses double opt-in; /latest shares posts.
  `projectx discord-commands` installs the slash commands. 95 backend tests.

- 2026-11-23 — Billing (M4) with Dodo Payments: hosted checkout for new subscriptions, in-place prorated
  plan changes, cancel at period end (and undo), customer portal. The plan changes only from verified
  Standard Webhooks, each recorded once (`billing_events`, migration 0003), applied in the tenant's own
  transaction. Payment trouble → past_due + alert; expiry → Free. Scale's 7% ad-spend share reported monthly
  as metered usage (USD only; other currencies flagged for a manual invoice, never silently converted).
  Plan limits are now actually enforced everywhere (they weren't): platforms, contacts (incl. import),
  landing pages, seats, posts and emails per month, with live counts and an 80% heads-up. Ads need Scale.
  Cockpit: Plan tab checkout/manage/cancel, "See plans" on any limit error. 91 backend + 12 web tests.

- 2026-11-23 — Ads engine (M4): Meta Marketing API (campaign → ad set → creative → ad, built paused and
  switched on last), Google Ads REST (link an existing campaign; budget, status, metrics; OAuth refresh),
  practice simulator. Caps in two places: our monthly cap checked at proposal *and* execution time, mirrored
  to the platform's spending limit where it exists. Starting ads and raising budgets always ask; lowering
  applies at once; stop rules pause at 2x target CPL for 3 days, after 3 days of budget with no results, and
  when the cap is reached. Politics never automated; housing/employment/credit can't use lookalikes. Goal
  guardrails (daily/total budget, CPL cap) enforced. Ads screen in the cockpit. CI fixed (it had never run)
  and green. Planner no longer schedules inside the next hour. 83 backend + 12 web tests.

- 2026-11-23 — Web cockpit shipped: design system (light/dark, Apple-style), hand-rolled SVG charts,
  auth + onboarding, Today, Decisions (edit-then-approve), Goals (live forecast, pace chart), Campaigns,
  Content (calendar), Channels, Audience (people/funnel/deals/payments), Inbox, Email, Pages & links
  (A/B results), Analytics, Brand, Settings. Verified in a real browser: 24 screens, 0 console errors,
  phone layout, approve-with-edit end to end. Also: inbox, compliance, media kit, seed-demo (practice
  company), A/B experiment results, SPA caching. Dev API port moved to 8010. 74 backend + 11 web tests.
  Next (M4): ads engine, billing (Dodo), Discord bot, asset library, WhatsApp, benchmarks; then M5
  Docker + deploy docs.

- 2026-11-23 — GTM backend core loop works end to end in practice mode: content pipeline (plan,
  write, adapt, check, propose), publisher with read-back verification, 10 platform adapters +
  simulator + Ayrshare, OAuth/token/practice accounts, newsletter digest, analytics, weekly report,
  public dashboard, cockpit briefing. 68 tests. Next: inbox + comment webhooks, experiments API,
  compliance (DSAR, data-deletion callback), media kit, ads engine, billing, Discord bot, seed demo,
  then the web cockpit.
- 2026-11-23 — Strategy layer: brand book, goals with forecasts and pacing, campaign library (33).
- 2026-11-23 — GTM data model, CRM funnel, nurture, links, landing pages, payments.
- 2026-11-23 — Spine: ledger, policy engine, jobs, vault, gateway, auth, tenancy (RLS).
