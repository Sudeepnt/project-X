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
- [x] M5 Hardening: security headers/CSP, rate limits, performance, docs, deploy guide

## Status log

(Newest first.)

- 2026-11-28 — Departments that work together, part 1: the bus and Finance. `spine/bus.py`: departments subscribe to
  ledger event types; appending an event queues one delivery per subscriber in the same transaction (exactly once,
  linked back through correlation/causation, chains capped at 40), and every handoff is logged in
  `department_handoffs`. Finance module (`modules/finance`): chart of accounts and balanced double-entry journal
  (idempotent per source), invoices with integer tax maths, P&L, balance sheet and runway from journal lines only.
  It reacts on its own: won deal → invoice drafted and proposed; payment recorded → matched to the oldest open invoice
  (remainder booked as a cash sale); billable milestone, approved payroll and subscription renewals → bills. Daily:
  overdue invoices get one reminder per step, are handed to Legal after 30 days, and a closed tax period becomes a
  manual filing task and then a payment proposal. Money out and tax filing always wait for a person. Migration `0010`.
  146 backend tests. Department plan (17 blueprint modules grouped): GTM (brand, content, marketing, PR, sales),
  Operations (support, delivery), Finance (billing, accounting, tax), People (payroll, HR), Legal and admin (contracts,
  IT, security), Product, Strategy (steering, network, handshake); contact platforms (WhatsApp, Gmail, Outlook) as a
  shared layer.

- 2026-11-28 — Our own memory, an always-on assistant, and open models first. Context store (`spine.context`):
  preferences, people, facts, routines and promises in PostgreSQL with full history (a change supersedes, never
  overwrites; forgetting wipes every version), visible and editable at `/context`. Every assistant message is learned
  from in the background by an open model; code keeps a note only if the person's own words are quoted as evidence
  and it isn't a secret; promises become follow-up reminders. Prompts get pinned notes plus the most relevant, bounded.
  Always on: per-person settings (brief time, days, quiet hours, channels, nudge cap); the assistant plans its own
  wakeups (morning brief, 30 minutes before meetings, midday, end of day, and right after a calendar sync changes busy
  time), computes signals from records (clashes, unapproved invites for meetings within a day, overdue reminders,
  promises, waiting decisions), says each once, stays quiet otherwise without calling a model, and delivers in-app and
  by email (other channels register a deliverer). An open model may smooth the wording only if every time and number
  is unchanged. Gateway: a `hard` rung for Claude; Anthropic models are refused for any job not marked hard.
  Migration `0009`. 142 backend tests.

- 2026-11-28 — The assistant (spine, backend only): `POST /assistant/messages` turns plain words ("move my 3pm to
  Friday and remind me to send the deck") into checked actions on the calendar and reminders. Built on Reflexion
  (Shinn et al., NeurIPS 2023): the model plans (Actor); plain code checks the plan against real records (Evaluator:
  the time is real and ahead, the event ref exists, the slot is free in our and connected calendars, each person
  resolves to exactly one email, never an invented one); on failure the model gets the failures, writes what it
  misread and retries, up to 3 trials, one model rung higher each time. When a wrong guess would be costly it asks
  instead. Last 6 turns are short-term memory; corrections ("no, morning for me is 11") become lessons and the 3 most
  relevant go into every prompt. Forwarded text is treated as information, never as instructions. Replies are built
  by code with the outcome first. Invites still wait on decision cards. Migration `0008`. 133 backend tests.
  Next: the same entry point from WhatsApp and email, a morning brief, and the screens.

- 2026-11-28 — Calendar sync with Google Calendar and Microsoft Outlook. Owners connect with OAuth (PKCE, one-time
  state, tokens only in the vault, refreshed before expiry, revoked access flags the connection and notifies once).
  Every 10 minutes the next 60 days of busy time are copied in as bare start/end pairs (no titles or attendees);
  free, cancelled and declined events don't count, and copies we placed ourselves are skipped so a moved meeting
  never clashes with its old copy. Free-time search and double-booking checks include this busy time. Our events are
  copied out with no attendees (the provider never emails anyone; invitations stay an approved action), updated on
  reschedule and deleted on cancel. Migration `0007`. 128 backend tests.

- 2026-11-28 — Calendar and reminders (spine, backend only; screen placement to be decided). Calendar: working
  hours per business, schedule / reschedule / cancel with double-booking checks, and a free-time finder (buffers,
  minimum notice, slots aligned to the local clock, correct across daylight-saving changes). Inviting people is the
  action `spine.calendar.send_invite` (Ask first by default): an email with an .ics file; each change raises the
  SEQUENCE, retires any unsent older card, and a cancellation only goes to people who were invited. Reminders: one
  table for anything (meetings, renewals, deadlines, payments, follow-ups) with lead times, repeats (kept on local
  wall-clock time), snooze and done; a periodic job fires them into notifications. `POST /reminders/capture` lets
  the model read any text and decide what to remember; code checks the date is real, quoted from the text, not
  past, and not already stored. Attendee emails are covered by contact export and erasure. Migration `0006`.
  125 backend tests. Next: Google / Microsoft calendar sync (busy times in, events out) and the screens.

- 2026-11-24 — Accessibility (M5 complete): axe-core audit, WCAG 2.2 AA plus best practices, on all 18 screens in
  light and dark mode — 203 contrast issues and 4 structural ones found, now zero. Text tokens darkened just enough
  (muted, faint, warning, success, error), accent split into a text colour and a fill colour so both pass in dark
  mode, accessible shades for platform marks, card titles as h2 under each page's h1, a main landmark on sign-in,
  labelled progress bars. Milestones M0–M5 are done.

- 2026-11-24 — A/B lab for posts: start a test on any platform version while a post awaits approval; version B
  (a linked sibling post) joins the same approval card and publishes at the same hour two days later. Every 6 hours
  results are evaluated as engagement per person reached; a winner needs 300+ reach on each and a 95% chance of
  being better, else "no clear winner" after 14 days. Results are labelled indicative and the winning opening is
  saved to memory for future planning. Analytics → A/B tests; Analytics tabs now follow the URL. Fix: the publish
  executor marked only the main post as scheduled. 114 backend tests. All GTM v1 extras now exist.

- 2026-11-24 — Budget planner: likely results at 0–3x a chosen budget and per paid channel, from the same Monte
  Carlo model as the goal forecast, with the cost of each extra result and a recommended budget (the most before
  extra results cost twice the first). The forecast now models diminishing returns on ad spend (a half-saturation
  daily spend per channel) instead of treating money as perfectly linear. Shown when setting a goal. 113 tests.

- 2026-11-24 — Repurposing (it was listed under M2 but wasn't actually built): paste an article, transcript or
  newsletter, choose 2–10 posts; a job splits it into distinct, standalone ideas (a verifier rejects duplicate
  angles), each written in the brand voice from the source alone, adapted per platform, checked and sent for
  review, spread one a day. Same piece isn't split twice. Content → Repurpose. Security pass over public
  endpoints (escaping, sandboxed templates, link destinations, webhook signatures): no issues. 112 backend tests.

- 2026-11-24 — Hardening: Discord /subscribe and /ask rate-limited per person and per server (stops using the
  bot to email-bomb strangers with confirmation mails); a periodic prune removes old throttle windows, spent or
  expired tokens and sessions, and finished jobs (the ledger is never pruned). 111 backend tests.

- 2026-11-23 — M5 deploy and hardening, part 1: one Docker image (non-root, health check, JSON logs) serving the
  API, cockpit and worker; docker-compose stack (Postgres + pgvector with a non-superuser role, migrate job,
  mail catcher); CI builds and smoke-tests the image. S3-compatible storage (encrypted, presigned URLs; AWS S3,
  R2, MinIO) next to local disk. `projectx generate-keys`. Production refuses plain-http web URLs, a dev mail
  host or S3 without a bucket. Security fix: the API trusted X-Forwarded-For from anyone (rate limits could be
  dodged); now only configured proxies. docs/DEPLOY.md and docs/architecture.md. 109 backend tests.
  Docker isn't installed on the dev Mac; the CI image job built and smoke-tested the image: green.

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
