# Deploying Project X

One container image runs everything. The same image starts as:

| Process | Command | Scale |
|---|---|---|
| API + cockpit | `projectx api --host 0.0.0.0 --port 8010` | horizontally, behind a load balancer |
| Worker + scheduler | `projectx worker` | 1 or more (the scheduler elects one leader through Postgres) |
| Migrations | `projectx migrate` | once per release, before the new API starts |

Stateful dependencies: **PostgreSQL 18 with pgvector**, **S3-compatible object storage** (once you run more than one
server), and an **SMTP provider**. There is no Redis: jobs, locks and caches live in Postgres.

## 1. Try it locally

```bash
docker compose up --build        # http://localhost:8010   ·  mail catcher on http://localhost:8025
docker compose run --rm api projectx seed-demo   # optional practice company
```

## 2. Before the first production deploy

1. **Generate secrets** and keep them in your secret manager:
   ```bash
   docker run --rm projectx:<tag> projectx generate-keys
   ```
   `PX_VAULT_MASTER_KEY` encrypts every stored platform token. **If you lose it, every connected account must be
   reconnected.** Back it up separately from the database.
2. **Create the database** with a non-superuser application role. Superusers bypass row-level security, which is
   what keeps businesses apart:
   ```sql
   CREATE ROLE projectx LOGIN PASSWORD '…' NOSUPERUSER NOBYPASSRLS;
   CREATE DATABASE projectx OWNER projectx;
   \c projectx
   CREATE EXTENSION citext; CREATE EXTENSION pgcrypto; CREATE EXTENSION vector; CREATE EXTENSION pg_trgm;
   ```
   (Extensions need a superuser once; the app never does.)
3. **Create a private bucket** (AWS S3, Cloudflare R2, MinIO, Backblaze B2). Block public access. Files are served
   only through short-lived presigned URLs and stored with server-side encryption.
4. **Pick a mail provider** (Postmark, SES, Resend, Mailgun…). Sign-up, password reset, invites, double opt-in
   and newsletters depend on it. Set SPF, DKIM and DMARC for the sending domain.

## 3. Configuration

Everything is an environment variable prefixed `PX_`. The service refuses to start in `staging`/`prod` with
unsafe values (development keys, plain-http URLs, no real mail server, S3 without a bucket).

**Required**

| Variable | Notes |
|---|---|
| `PX_ENV` | `prod` (or `staging`) |
| `PX_BASE_URL` | Public https URL of the API: tracked links, landing pages, OAuth callbacks, webhooks |
| `PX_WEB_URL` | Public https URL of the cockpit (same as `PX_BASE_URL` when the API serves it) |
| `PX_DATABASE_URL` | `postgresql+asyncpg://projectx:…@host:5432/projectx` (TLS: add `?ssl=require`) |
| `PX_SECRET_KEY`, `PX_VAULT_MASTER_KEY` | from `projectx generate-keys` |
| `PX_SMTP_HOST`, `PX_SMTP_PORT`, `PX_SMTP_USERNAME`, `PX_SMTP_PASSWORD`, `PX_SMTP_STARTTLS`/`PX_SMTP_USE_TLS`, `PX_EMAIL_FROM` | mail provider |
| `PX_BLOB_BACKEND=s3`, `PX_S3_BUCKET`, `PX_S3_REGION`, `PX_S3_ACCESS_KEY_ID`, `PX_S3_SECRET_ACCESS_KEY` | `PX_S3_ENDPOINT_URL` for non-AWS stores |
| `PX_FORWARDED_ALLOW_IPS` | your load balancer's IPs/CIDRs, so rate limits see real client IPs. Never `*` if the API is reachable directly |

**AI models** (without keys the app uses deterministic offline drafts)

`PX_ANTHROPIC_API_KEY`; `PX_LLM_SMALL`, `PX_LLM_MID`, `PX_LLM_FRONTIER` as `provider:model`, e.g.
`anthropic:claude-haiku-5-5`, `anthropic:claude-sonnet-5-5`, `anthropic:claude-opus-5-5`.
`PX_LLM_DEFAULT_DAILY_CAP_USD` bounds spend per business per day on top of each plan's cap.

**Platforms** (each is optional; unset ones show "not configured" in Channels)

| Platform | Variables | Callback / webhook URL to register |
|---|---|---|
| Meta (Facebook, Instagram), WhatsApp | `PX_META_APP_ID`, `PX_META_APP_SECRET`, `PX_META_WEBHOOK_VERIFY_TOKEN` | OAuth `{BASE}/oauth/facebook/callback`, `{BASE}/oauth/instagram/callback`; webhook `{BASE}/webhooks/meta`; data deletion `{BASE}/webhooks/meta/data-deletion` |
| Threads | `PX_THREADS_APP_ID`, `PX_THREADS_APP_SECRET` | `{BASE}/oauth/threads/callback` |
| X | `PX_X_CLIENT_ID`, `PX_X_CLIENT_SECRET` | `{BASE}/oauth/x/callback` |
| LinkedIn | `PX_LINKEDIN_CLIENT_ID`, `PX_LINKEDIN_CLIENT_SECRET` | `{BASE}/oauth/linkedin/callback` |
| TikTok | `PX_TIKTOK_CLIENT_KEY`, `PX_TIKTOK_CLIENT_SECRET` | `{BASE}/oauth/tiktok/callback` |
| YouTube, Google Ads | `PX_GOOGLE_CLIENT_ID`, `PX_GOOGLE_CLIENT_SECRET`, `PX_GOOGLE_ADS_DEVELOPER_TOKEN` | `{BASE}/oauth/youtube/callback` |
| Google Calendar | same Google client; enable the Google Calendar API, scope `calendar.events` | `{BASE}/oauth/calendar/google/callback` |
| Microsoft Outlook calendar | `PX_MICROSOFT_CLIENT_ID`, `PX_MICROSOFT_CLIENT_SECRET` (Entra app, any account type, delegated `Calendars.ReadWrite`, `User.Read`, `offline_access`) | `{BASE}/oauth/calendar/microsoft/callback` |
| Pinterest | `PX_PINTEREST_APP_ID`, `PX_PINTEREST_APP_SECRET` | `{BASE}/oauth/pinterest/callback` |
| Telegram | `PX_TELEGRAM_WEBHOOK_SECRET` | set per bot automatically |
| Discord bot | `PX_DISCORD_APPLICATION_ID`, `PX_DISCORD_PUBLIC_KEY`, `PX_DISCORD_BOT_TOKEN`, `PX_DISCORD_CLIENT_SECRET` | interactions `{BASE}/webhooks/discord/interactions`; redirect `{BASE}/oauth/discord/bot/callback`; then run `projectx discord-commands` once |

**Billing (Dodo Payments)**: `PX_DODO_API_KEY`, `PX_DODO_WEBHOOK_SECRET`, `PX_DODO_ENVIRONMENT=live_mode`,
`PX_DODO_PRODUCT_IDS={"autopilot":"pdt_…","growth":"pdt_…","scale":"pdt_…"}`, and `PX_DODO_AD_SHARE_EVENT` for the
usage meter. Webhook: `{BASE}/webhooks/billing/dodo`.

Customers' own payment providers (Stripe, Razorpay, Dodo) are set per business in Settings → Business → Payments;
their webhook URL is shown there.

## 4. Releasing

1. Build and push the image (CI builds and smoke-tests it on every push to `main`).
2. Run `projectx migrate` with the new image. Migrations are additive and safe to run while the old version serves.
3. Roll the API, then the workers. Workers finish or release their current job on SIGTERM; leases expire on their
   own if a worker dies, and another worker picks the job up.

Health checks: `GET /healthz` (process up) and `GET /readyz` (database reachable). Use `/readyz` for load-balancer
readiness and `/healthz` for liveness.

## 5. Operating it

- **Backups**: daily Postgres snapshots plus point-in-time recovery; versioning on the bucket. Test a restore.
  Back up `PX_VAULT_MASTER_KEY` separately.
- **The ledger** is append-only and hash-chained per business. `projectx verify-ledger <business_id>` (or
  Settings → Activity log → Verify) proves it hasn't been altered.
- **Logs** are structured JSON in production (`PX_LOG_JSON=true`), one line per request with a request id that is
  also returned in the `X-Request-Id` header and in error responses.
- **Scaling**: API instances are stateless. Add workers when jobs queue up (`SELECT kind, count(*) FROM jobs WHERE
  status = 'queued' GROUP BY kind`). Each worker runs `PX_WORKER_CONCURRENCY` jobs at once.
- **Secrets rotation**: rotating `PX_SECRET_KEY` signs everyone out and invalidates outstanding email links;
  rotating the vault key re-wraps each business's data key (no re-encryption of every secret).
- **Data requests**: exports and erasures run from Settings → Privacy, and Meta's data-deletion callback is
  handled automatically.

## 6. Security checklist

- [ ] TLS everywhere; HSTS on the load balancer
- [ ] App role is `NOSUPERUSER NOBYPASSRLS`; database not reachable from the internet
- [ ] Bucket private, encrypted, versioned
- [ ] `PX_FORWARDED_ALLOW_IPS` set to the load balancer only
- [ ] Secrets in a secret manager, not in the image or repository
- [ ] Platform apps in live mode only after their reviews pass; webhooks verified (all handlers check signatures)
- [ ] Alerts on 5xx rate, job failures (`status = 'dead'`) and `/readyz`
