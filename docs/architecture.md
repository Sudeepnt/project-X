# Architecture

Project X is one product made of a shared **spine** and department **modules**. The spine owns everything
that must behave the same for every department: who you are, which business you're in, what the system may do
on its own, what it did, how it pays for AI, and how work runs in the background. Modules own their domain
(GTM today; finance, support and others later) and plug into the spine through a small, explicit interface.

```
                        ┌──────────────────────── Cockpit (React SPA) ────────────────────────┐
                        │ Today · Decisions · Goals · Campaigns · Content · Channels · Ads ·   │
                        │ Audience · Inbox · Email · Pages · Analytics · Brand · Settings      │
                        └───────────────▲─────────────────────────────────────▲───────────────┘
                                        │ JSON (cookie session + CSRF)        │ static files
┌───────────────────────────────────────┴─────────────────────────────────────┴───────────────┐
│ API (FastAPI)            /api/v1/b/{business}/…      public: /l /p /c /u /oauth /webhooks   │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│ Modules        gtm: brand · goals · campaigns · content · channels · ads · crm · nurture ·   │
│                     growth · inbox · analytics · benchmarks · assets · compliance · discord  │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│ Spine   identity · tenancy (RLS) · policy engine · decisions · autonomy · ledger · jobs ·    │
│         scheduler · vault · model gateway · notifications · billing · memory · twin          │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│ PostgreSQL 18 (+pgvector)          S3-compatible storage          SMTP          AI providers  │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
        ▲ the same image also runs `projectx worker`: jobs, the scheduler, daily/weekly cycles
```

## The rules the code enforces

**Businesses are sealed from each other.** Every table with a `business_id` has PostgreSQL row-level security
enabled *and forced*, keyed to a per-transaction setting. The app connects as a role that cannot bypass it. A
test fails if a new tenant table forgets RLS. Public endpoints (links, pages, webhooks) first resolve the owning
business through `routing_keys`, then open a tenant transaction. One platform account can belong to only one
business at a time.

**The AI proposes; plain code decides what runs.** Every action an agent can take is a registered
`ActionType` with a ceiling. The policy engine computes a level, `Never < Draft < Ask first < Run and tell <
Run`, as the minimum of: autonomy earned for that action, its ceiling, the founder's caps, the goal's approval
mode, money limits, and hard rules (plan limits, guardrails, brand checks). Public, costly or irreversible actions
can't run without a person. Proposals become **decisions** (approve, edit, decline, undo); executors and
undoers are registered per action. Autonomy is earned through clean streaks the founder must accept, and is
lost at once on incidents.

**Everything is on the record.** The ledger is append-only and hash-chained per business. A trigger rejects
`UPDATE`/`DELETE`/`TRUNCATE`; event types are registered with schemas that exclude personal data, so erasure
requests never conflict with the ledger.

**Agents never hold keys.** Credentials are sealed with AES-256-GCM under a per-business data key that is
wrapped by a master key (envelope encryption), bound to their row as associated data. Every read is logged
with its purpose.

**Numbers come from code; words from models.** Reports, forecasts and analytics are computed first, then narrated.
The model gateway routes each call to the cheapest rung (small, mid, frontier) that passes its schema and
verifier checks, climbs only on failure, caches results, records every call's cost, and enforces a daily cap
per business. With no API key it runs fully offline with deterministic drafts.

## Background work

Jobs live in Postgres and are claimed with `FOR UPDATE SKIP LOCKED`, with leases and heartbeats, fencing,
idempotency keys and exponential backoff (`RetryLater` for rate limits, `PermanentFailure` for hopeless cases).
A scheduler elected through an advisory lock enqueues each business's daily and weekly cycle in its own time
zone. Workers drain in-flight jobs on SIGTERM.

## How a GTM post goes out

1. The daily cycle paces the goal and asks each active campaign to plan the week (`plan_content`). Slots never
   fall inside the next hour, so there's always time to review.
2. Each idea is written once in the brand voice (`write_core`), then adapted per platform (`adapt_variants`).
3. Checks run: brand voice and banned words, claims that need proof, near-duplicates (SimHash), platform specs.
4. A `gtm.publish_post` proposal goes through the policy engine. In "approve everything" mode it becomes a
   decision card where the founder can edit each platform's text before approving.
5. The publisher posts through the platform adapter (or the simulator in practice mode), respecting daily limits
   and plan quotas, verifies the post by reading it back, and records it.
6. Metrics sync later; posts are scored against the account's own average; pattern memory (hooks, themes,
   formats, hours, platforms) feeds the next plan. Weekly reports are computed, then narrated.

## Money

Ads start and budgets rise only with a person's approval. Each ad account has a monthly cap that is checked when
the action is proposed and again when it executes, and is mirrored to the platform's own spending limit where one
exists. The daily job pauses everything the moment the cap is reached, and proposes pauses for campaigns that
miss their stop rules. Customer billing (Dodo Payments) changes a plan only from verified webhooks, each
recorded once. Plan limits are enforced from live counts.

## Privacy

Consent is recorded with evidence per channel and purpose; double opt-in is supported; every email carries
one-click unsubscribe (RFC 8058) and a postal address; WhatsApp STOP withdraws consent. Exports and erasures
are self-serve, and Meta's data-deletion callback is implemented. Benchmarks are opt-in, anonymous (salted
contributor ids, no business id) and shown only when at least five businesses contribute. Uploaded JPEGs are
stripped of EXIF metadata.

## Adding a module

A module is a package that registers, at import time: its models (tenant tables get RLS in its migration), its
event types, its action types and executors, its jobs, and a `Module` describing its routers, public routers,
daily/weekly cycle hooks, briefing items, the numbers it contributes to the cockpit, and live usage counts for plan
limits. The spine never imports a module directly; it calls the hooks.

## Departments and how they hand work to each other

Each department is a module under `backend/src/projectx/modules/`. They never call each other directly: each one
appends events to the ledger and subscribes to the events it cares about (`spine/bus.py`). Every delivery is logged in
`department_handoffs`. Anything outward, costly or irreversible still goes through the policy engine and a decision
card.

| Event | Who reacts, and how |
|---|---|
| `gtm.deal.changed` (won) | Finance drafts the invoice · Operations starts the project and books the kickoff · Legal drafts the agreement |
| `gtm.payment.recorded` | Finance matches it to open invoices and books it |
| `gtm.contact.erased` | Operations erases ticket text |
| `ops.milestone.completed` | Finance invoices the milestone |
| `ops.feedback.received` | Product links it to a roadmap item |
| `product.feedback.linked` / `product.item.shipped` | Operations remembers the item / tells every customer who asked |
| `product.release.published` | GTM drafts the announcement |
| `finance.invoice.escalated` | Operations puts the customer's projects on hold · Legal drafts a formal demand |
| `finance.bill.paid` (payroll) | People emails payslips |
| `people.employee.hired` | Legal drafts the employment contract and NDA, and account set-up tasks |
| `people.employee.leaving` | Admin creates a removal task for every access, flags assets to reassign |
| `people.payroll.approved` | Finance books payroll and proposes the payment |
| `admin.asset.renewing` | Finance books the bill and proposes paying it |
| weekly | Strategy reads every department's numbers, checks cross-department risks, writes the review |

People decide only the one-way doors: money out (bills, payroll), signing contracts, filing tax, formal demands, and
anything sent to someone outside the business until that action has earned the right to run alone.

Handshake (`modules/network`): two businesses on the platform link their assistants with a one-time code; each side
grants scopes (today only `availability`, which means free/busy). A meeting request finds the first slot both are free,
the other business approves it, and it is booked on both calendars. A request beyond the granted scope is refused,
logged and reported to that business.
