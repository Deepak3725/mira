# MIRA Deployment Guide (free tier, $0 infrastructure)

Target: small public beta on **Render Free** (app) + **Neon Free**
(PostgreSQL). Verified against official docs on 2026-10-30; quotas move,
so re-check the Usage pages the week you launch.

This is NOT commercial-grade hosting: expect sleep/wake latency, modest
quotas, and no uptime SLA. Do not present it as 100,000-user capacity.

## 1. Architecture

- FastAPI + Jinja app on one Render free web service (HTTPS included).
- All persistent data in Neon Postgres via `DATABASE_URL`.
- Render's filesystem is **ephemeral**: anything written to local disk
  (SQLite files, uploads) is lost on restart/redeploy. The app therefore
  never stores persistent data on disk in production.
- Sessions are stateless HMAC cookies; no session store to migrate.
- Secrets live only in Render environment variables, never in the repo.

## 2. Owner setup (D0) — do this first

### Neon (database)

1. Sign up at https://neon.com (Free plan is permanent, no card required).
2. **STOP if Neon asks for a payment card** and report it as a blocker.
3. Create a project, e.g. `mira-prod`. Copy the connection string
   (it ends with `?sslmode=require`).
4. Create a second branch, e.g. `staging`, for tests and restore drills.
   Copy its connection string separately.

### Render (app hosting)

1. Sign up at https://render.com and connect the MIRA GitHub repo.
2. **STOP if Render asks for a payment card** and report it as a blocker.
3. New -> Blueprint, select the repo (`render.yaml` is at the root).
4. When prompted for unsynced variables:
   - `DATABASE_URL`: paste the **Neon prod** connection string.
   - `MODEL_API_KEY`: leave empty (mock mode by default).
5. Confirm the service plan shows **free**. Never link a payment method:
   without one, over-quota services spin down instead of billing.

## 3. Environment variables

| Variable | Prod value | Notes |
|---|---|---|
| `APP_ENV` | `production` | Enables secure cookies, demo blocks, secret checks |
| `DATABASE_URL` | Neon prod string | Empty = local SQLite (dev only) |
| `SESSION_SECRET` | generated (32+ chars enforced, 40+ recommended) | Render `generateValue`; app refuses to boot without it |
| `AI_PROVIDER` | `mock` | Set `meta` only to deliberately enable live AI |
| `META_MODEL` | empty until needed | e.g. `muse-spark-1.3` |
| `MODEL_API_KEY` | empty until needed | Never commit, never log |
| `PYTHON_VERSION` | `3.13.5` | 3.13.x line (local dev runs 3.13.9) |

Local testing against Postgres: set `MIRA_TEST_DATABASE_URL` to the **Neon
staging** string in your local `.env`, then run the suite. Tests wipe and
recreate the `mira_test` schema on every `setUp` — never point it at prod.

## 4. Migrations

- The app runs `init_db()` on boot: on Postgres it creates any missing
  tables/indexes idempotently and stamps `schema_version`.
- Additive column changes ship as `_ensure_column`-style migrations in code.
- Before deploying a version with schema changes: apply it to the Neon
  staging branch first (point a local boot at it or run the suite), then
  deploy; the production boot migrates automatically.
- Rollback: redeploy the previous Render release. Schema changes in this
  project are additive, so old code keeps working.

## 5. Backup, export, recovery

- **Backup:** `pg_dump "$DATABASE_URL" > mira-YYYY-MM-DD.sql`
  (needs local Postgres client tools OR run from any machine with them).
- **Restore drill:** create a fresh Neon branch, `psql <staging-url> <
  mira-YYYY-MM-DD.sql`, then compare row counts:
  `SELECT 'users', COUNT(*) FROM users UNION ALL SELECT 'twins',
  COUNT(*) FROM twins;` (extend per table) against production.
- **Per-user export:** account data can be exported as JSON on request
  (all profile/memory tables are keyed by user/twin id).
- Neon free keeps a 6-hour point-in-time history (1 GB limit) — not a
  substitute for your own dumps. Take a dump before every risky change.

## 6. Monitoring (all free, all in dashboards)

- Render Dashboard -> service Metrics: CPU/RAM, request count, **outbound
  bandwidth** (Hobby included: 5 GB/month; over-quota without a card =
  services sleep until next month).
- Render Dashboard -> Billing: free instance hours (750/month; spun-down
  services consume none).
- Neon Dashboard -> Usage: storage (1 GB/project), compute CU-hours
  (100/project/month), egress (5 GB/project).
- In-app `/usage` page: Meta AI spend from the app's own log (estimates,
  not Meta's authoritative balance).

## 7. Honest free-tier limits

- First request after 15 idle minutes takes ~1 minute (Render cold start),
  plus a few seconds for Neon to scale compute back up (5-min idle scale
  to zero, always on for free).
- 512 MB RAM / shared CPU on Render free: fine for a beta, not for heavy
  concurrency. One web instance only.
- Neon free: no scheduled snapshots, 6-hour history only, 10 branches.
- Supabase is NOT used (smaller free DB, weekly project pauses) unless a
  real blocker forces reconsideration.

## 8. Security posture for the public beta

- CSRF tokens on all 23 POST routes; `Secure`/`HttpOnly`/`SameSite=lax`
  cookies in production; generic error pages (no tracebacks).
- `SESSION_SECRET` required (minimum 32 chars enforced, 40+ recommended) or the app refuses to boot.
- Demo seeding refuses to run in production; `@example.com` demo logins
  are rejected in production.
- Registration/login rate-limited per IP (in-memory; resets on restart —
  acceptable for a single-instance beta, documented limitation).
- No CSRF-exempt state-changing endpoints; JSON message polling is GET-only.
- Remaining gaps before any larger launch: DB-backed rate limits,
  password-reset flow, audit-log retention policy, abuse monitoring.
