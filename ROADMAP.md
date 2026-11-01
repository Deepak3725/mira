# MIRA Development Roadmap — AI Capabilities Programme

Direction (2026-10-31): hosting is DEPRIORITIZED. Do not start Render
deployment. Priority is AI capabilities + production-ready architecture
for future public hosting when funded.

## How to resume

If context is lost: read this file, then continue from the
**last verified checkpoint** below. Do not re-run completed phases;
verify the checkpoint command first.

- Last verified checkpoint: `P7-twin-flows` (2026-11-01) —
  394/394 non-E2E green (352 + 42 P7), SQLite-forced,
  sandboxed, ~105s; plus 9/9 E2E green unsandboxed (403 total).
- Verify with: `MIRA_TEST_DATABASE_URL=sqlite DATABASE_URL=''`
  `.python\python.exe -m unittest` on the 18 non-E2E modules
  (add `tests.test_twin_flows`).
  E2E need an unsandboxed runner (TestClient sockets); last run
  9/9 OK on 2026-11-01.
- FEATURE FREEZE in effect from 2026-11-01: no more product
  features. Remaining work is integration testing, critical bug
  fixes, Render prep, GitLab upload, demo video, submission.

## Phase 0 — Audit snapshot (2026-10-31)

Method: 6-scope parallel agent audit (web-auth, data-layer, tests,
ai-provider, engines, ops-config) + critic + execution follow-up,
all findings file-grounded. Prior verified runs: 167/167 SQLite and
168/168 Neon PG (unsandboxed, earlier session).

Baseline (verified this session, sandboxed):
`Ran 159 tests in 32.853s OK` (10 non-E2E modules). The 9 E2E tests
fail in-sandbox only (WinError 10013 socketpair block — environmental,
not code; first-test ERROR skips tearDown and cascades via the
`test_e2e_flows.py:59` settings guard).

### Implemented inventory (reuse all of this)

- Web/auth: FastAPI + 7 routers, server-rendered HTML + 303s;
  PBKDF2-HMAC-SHA256 200k + per-user salt; stateless HMAC session
  cookie (7-day); CSRF `require_csrf` on every POST route + token in
  every form; hand-rolled `_clean`/allow-lists; in-memory
  sliding-window rate limits; Jinja autoescape.
  Key files: `app/main.py`, `app/auth.py`, `app/routes/*.py`.
- Data: 10 tables + 13 indexes, `SCHEMA_VERSION=7`, single-source
  `app/schema.py` + dual-driver `app/db.py` (SQLite files / pooled
  psycopg, `mira_test` test schema); per-request connections;
  engine+route isolation (conversations/requests/memories);
  field-level `privacy.py` allowlists; idempotent `seed.py` with
  production refusal.
- AI: `MetaProvider` (stdlib urllib Chat Completions, 30s timeout,
  refuses unless priced model + key); `MockProvider` default;
  `budget.py` caps (monthly/user-daily/per-conversation) +
  `usage_log` accounting (`ok`/`failed_known`/`failed_unknown`);
  meta→mock fallback with visible notice; M1 intent
  (deterministic-first + one low-temp structured call).
  Key files: `app/meta_client.py`, `app/providers.py`,
  `app/mock_mind.py`, `app/budget.py`, `app/intent.py`.
- Engines: request lifecycle (`app/requests.py`),
  deterministic parser (`app/request_parse.py`), twin-to-twin
  conversations (`app/conversations.py`), matching
  (`app/matching.py`), twins CRUD + conditions + visibility,
  memories (pending→approved→rejected), ChatGPT export import
  (`app/import_pipeline.py`, pending-only).
- Ops: stdlib `Settings` + `.env`, production boot guard,
  generic error pages, `render.yaml` free service, `DEPLOY.md`
  runbook, `/healthz`, `audit_events`, partial export/deletion.
- Tests: 168 tests in 11 files, stdlib unittest; AI always mock
  in-suite (fake `post=`); PG mode via `MIRA_TEST_DATABASE_URL`.

### Gap register

| ID | Severity | Gap | Status |
|----|----------|-----|--------|
| G1 | High | `/usage` shows GLOBAL spend to any logged-in user | fix in P0 |
| G2 | High | PG init skips `_ensure_column` (new columns never backfilled) | fix in P0 |
| G3 | High | Sessions unrevocable 7-day; no rotation; anon CSRF token global | P0 partial → P5 |
| G4 | Med | No security headers; `/docs`+`/openapi.json` public | fix in P0 |
| G5 | Med | Password no max length (PBKDF2 CPU-DoS); no reset/verify/lockout | P0 partial → P5 |
| G6 | Med | SQLite no WAL/busy_timeout; PG pool max4 process-global | P0 |
| G7 | Med | Rate limits in-memory, per-process; many POSTs unlimited | P0 partial → P4 |
| G8 | Med | No provider ABC/registry/retry/latency; Meta-only pricing | Phase 1 |
| G9 | Med | No `get_provider`/AI_PROVIDER routing tests; no Meta-at-HTTP tests | Phase 1 |
| G10 | Med | Import: ChatGPT-only, `.json` ext check only, no MIME/quota/scan | Phase 2 |
| G11 | Med | No opportunity-create route → empty marketplace on fresh prod | Phase 5 |
| G12 | Med | No logging/metrics/tracing; no retention/purge policy | P0 partial → P3 |
| G13 | Low | Unpinned deps; no CI/Dockerfile; manual backup/drill | P0 partial |
| G14 | Low | DDL duplicated (init_db vs schema module); parity by test only | P0 |
| G15 | Low | `sample_export.json` fixture dead to suite; truncation alters data | P0/Phase 2 |
| G16 | — | Execution/messaging/voice entirely absent (greenfield) | Phases 3/4/6 |

### Product decisions (safe defaults, reversible)

- D1: `/usage` becomes per-user scoped (closes G1). Global view needs
  an explicit owner override later.
- D2: `/docs` + `/openapi.json` disabled when `APP_ENV=production`.
- D3: Unknown `AI_PROVIDER` names keep failing safe to mock, but now
  log a visible warning (no silent surprise).
- D4: No live Meta calls in the committed suite (existing design kept);
  real-call checks stay manual/opt-in.

## Phase plan

### P0 — Hardening (before features) [DONE 2026-10-31]

- P0a: per-user `/usage` (`scope=mine`, template line) + tests; global
  form kept for tests/future admin; `docs_url=None` in prod + tests.
- P0b: `_ENSURE_COLUMNS` spec shared by both drivers; PG init now
  backfills; SQLite DDL deduped into `schema.statements('sqlite')`.
- P0c: SQLite WAL + `busy_timeout=5000`; rate limits added to twin
  CRUD, memory CRUD, request confirm/close, convo controls
  (statically pinned); single-worker constraint documented in
  code comments; logout intentionally unlimited (harmless).
- P0d: `MAX_PASSWORD_LENGTH=128` at register + login; 6 MB body
  cap middleware (413); upload MIME allowlist + `accept_upload`
  helper; per-user import quota (10/h). CSRF-per-session rotation
  DEFERRED to P5 (needs unsandboxed E2E verification).
- P0e: `app/log.py` (redacting JSON logs, request-ID middleware);
  `app/retention.py` (`purge_old_rows`, allowlisted tables).
- P0f: `sample_export.json` pinned in suite. Unsandboxed full-run
  still pending (needs approval to run unsandboxed).
- Result: 20 new tests, 179/179 green, no regressions.

### P1 — Multi-provider AI orchestration [DONE 2026-10-31]

- New `app/ai/` package: `types` (AIResult/ToolCall/TaskContext/
  ProjectContext/Capabilities/errors), `registry` (5 vendors,
  independent enablement, unknown→mock+warning), `router`
  (`route()` + `run_task()` with budgets→retry→telemetry→
  accounting), `retry` (backoff + Retry-After), `telemetry`.
- Adapters: Meta (wraps verified chat client + Responses-API
  tool loop per official docs), OpenAI, Anthropic, Gemini
  (text + structured; tools for OpenAI/Anthropic; Gemini
  tools honestly refused). All stdlib urllib, fake transports
  in tests. No keys configured: third-party live calls are
  NOT yet verified (reported, not claimed).
- Pricing honesty: `VENDOR_PRICES` starts empty; unpriced
  vendors refused BEFORE network; mock=$0; Meta prices kept.
- Schema v8: `usage_log.latency_ms`/`attempts` (+ensure specs,
  both drivers); `/usage` shows avg latency; failures keep
  failed_known/failed_unknown rows with mock fallback labelled.
- Legacy chat/intent paths untouched (all old tests pass as-is).
- Result: 34 new tests, 213/213 green, no regressions.

### P2 — Universal knowledge import [DONE 2026-10-31]

- Researched real formats: ChatGPT mapping (existing), Claude
  conversations/memories/projects.json (schema survey + official
  export route), Gemini Takeout MyActivity (real-export analysis),
  Meta AI (no machine export found → honest unsupported slot).
- New `app/importers/`: base/streaming/archive/registry + 5
  importers; owner-words-only; timestamps + attachment counts;
  7-category deterministic classifier; near-dupe conflict
  warnings; zip multi-member merge with bomb guards; bounded
  incremental parsing with early stop; `.json` + `.zip` uploads.
- Schema v9: `memories.category`/`source_time`; category filter
  in review UI; richer import notice; template keeps honest
  manual-only wording (no sync/scrape claims).
- Legacy `parse_chatgpt_export` behaviour preserved exactly.
- Result: 29 new tests, 242/242 green, no regressions.

Reuse: `import_pipeline.py`, `memories.py` pending/approve,
routes/templates, `test_phase4`.
New: importer ABC + registry; Claude/Gemini/Meta-AI parsers from
real export formats; streaming large files; categories
(skills/experience/preferences/projects/interests/style);
dupe/conflict detection; approve/edit/reject/delete/re-import.
Done when: green; owner-review preserved; no scrape/sync claims.

### P3 — Project execution engine [DONE 2026-10-31]

- Schema v10: projects, project_tasks, project_files (UNIQUE
  per version), project_events audit.
- New `app/projects/`: DAG (`topo_sort`, cycle names, ready
  sets); spec builder + clarifier + strict AI-upgrade;
  kind-based assignment via Phase 1 router (image tasks
  honestly refuse); versioned jailed workspace; restricted
  runner (approval-gated, argv allowlist, temp jail, env
  scrub, timeouts, output caps; container isolation honestly
  documented as unavailable); engine (lifecycle, advance,
  failure routing, review loop, pause/resume/cancel,
  approval-gated checks, finalize report with deliverables).
- Routes + templates for the whole flow; owner-only, CSRF,
  rate-limited, statically pinned.
- Result: 26 new tests, 268/268 green, no regressions.

New: spec builder + clarifier; task DAG; agent assignment;
model routing per task kind; shared workspace + file versions;
sandboxed code exec (timeouts, limits, audit); failure
routing; review loop; final report + approvals; pause/resume.
Safety: never run untrusted commands on host; no overwrite
without approval/versioned backup.
Done when: green; e-commerce-style demo runs on mocks.

### P4 — External messaging framework [DONE 2026-11-01]

- Investigated official APIs: Telegram Bot API implemented
  (token flow, no review); WhatsApp/Messenger/Instagram need
  Meta app + review + assets (absent) so sends honestly refuse
  with exact setup text; webhook HMAC verify + defensive
  parsing implemented for all.
- New `app/channels/`: ABC, Telegram, Meta trio, mock;
  tz-aware scheduler (past→tomorrow); exact-then-substring
  recipient resolution (ambiguity refuses); outbox engine
  (accounts/contacts/drafts/schedule/send/process/retry/
  cancel/permissions/full audit); persistent queue with
  explicit triggers (no background worker on free tier).
- Schema v11. Routes + templates + webhooks (POST
  verify-then-parse, GET Meta handshake). Tokens server-side
  only; receipts required for 'sent'.
- Security-wiring test extended for the webhook HMAC model
  (documented; still requires authentication per POST).
- Result: 20 new tests, 288/288 green, no regressions.

New: channel ABC; WhatsApp/Messenger/Instagram/Telegram via
official APIs only; OAuth/linking; token store + revocation;
webhook verify; drafts + delivery status; persistent job queue;
tz-aware scheduling; retries + idempotency; per-account perms;
outgoing audit log. Default: show-before-send; never ambiguous.
Done when: green; no message claimed sent without confirmation.

### P5 — Twin intelligence + network + supply side [DONE 2026-11-01]

New: owner-approved knowledge in answers; discovery + negotiation
within MIRA; authorised-only exchange; intervention requests.
Also: opportunity-create UI/API (fixes G11); visibility HTTP
tests; session/password hardening remainder (G3/G5).
Constraint: human approval model preserved — no hire/buy/pay/
sign/commit by twins.
Done when: green; cross-user access tests extended.

### P6 — Voice interface [DONE 2026-11-01]

Investigate Meta Voice Transcribe + low-cost/free options first.
New: STT/TTS, NLU, multi-step + clarification, interruptions,
status updates, voice project/messaging commands, confirmations,
text fallback.
Done when: green with mocks; real service optional.

## Cost ledger (Meta API)

- Reported balance at start: ~$148. Reserve floor: $30 (stop
  before falling below; warn when approaching).
- Personal spending: $0. No paid services/cards/billing.
- Spend log: P0–P6 programme $0.00 (mock-only by design).
  P7 live validation spent ~$0.008 (4 tiny Meta calls:
  chat $0.0006, structured $0.0018, tool loop $0.0050,
  project agent $0.0007). Prior Oct-30 verification: $0.01148.
  Balance unverifiable programmatically (no balance endpoint);
  owner-reported ~$148 with Nov-4 expiry (owner-reported).
  P6 voice runs $0 via browser speech; Meta transcription stubbed.

## Checkpoint log

- `P0-baseline` 2026-10-31: 159/159 non-E2E OK (32.9s,
  SQLite-forced, sandboxed). E2E 9 unrun here (needs
  unsandboxed sockets); earlier verified 168/168 Neon PG.
- `P0-hardening` 2026-10-31: 179/179 non-E2E OK (28.9s).
  Usage scoping, docs lockdown, PG ensure parity, DDL dedupe,
  WAL/busy_timeout, rate limits, password cap, body cap, MIME
  checks, redacting logs, retention, fixture pin. No regressions.
- `P1-orchestration` 2026-10-31: 213/213 non-E2E OK (~95s).
  `app/ai/` registry/router/4 adapters + mock, retry/backoff,
  telemetry, v8 columns, run_task orchestration. Third-party
  live calls explicitly NOT verified (no keys, $0 spend).
- `P2-import` 2026-10-31: 242/242 non-E2E OK (~99s).
  `app/importers/` 5 vendors, zip merge, streaming, categories,
  conflicts, v9 columns. Legacy import behaviour preserved.
- `P3-execution` 2026-10-31: 268/268 non-E2E OK (~106s).
  `app/projects/` DAG/spec/assign/workspace/runner/engine +
  routes/templates, v10 tables. Container isolation honestly
  out of scope (approval-gated restricted runner instead).
- `P4-messaging` 2026-11-01: 288/288 non-E2E OK (~59s).
  `app/channels/` 5 vendors, scheduler, outbox, webhooks,
  routes/templates, v11 tables. Meta sends need credentials
  (honest stubs); delivery only on explicit triggers.
- `P5-twin` 2026-11-01: 311/311 non-E2E OK (~101s).
  Session-bound CSRF rotation + logout revocation (v12
  `revoked_sessions`), `app/twin_mind.py` approved-only
  answers with citations, `GET/POST /opportunities/new`
  supply side, security-headers middleware. 23 new tests in
  `tests/test_phase5_twin.py` (real direct-call CSRF
  roundtrips, revocation, twin-mind read-only proof).
- `P6-voice` 2026-11-01: 352/352 non-E2E OK (~98s).
  `app/voice/` (browser/text/mock providers, Meta transcribe
  honest stub, deterministic command parser, turn handler with
  confirm/cancel/interrupt + per-user pending state), `/voice`
  page (Web Speech API + typed fallback), `POST
  /voice/command` JSON with session CSRF. Fixed P4 `send_now`
  missing-conn TypeError + regression test. No new tables.
  Meta Voice Transcribe investigated (real: $0.18/hr via Meta
  Model API); no live calls, suite stays offline.
- `P7-twin-flows` 2026-11-01: 394/394 non-E2E OK (~105s) +
  9/9 E2E OK unsandboxed. Conversational twin setup
  (`app/twin_setup.py`), NL twin updates (`app/twin_update_nl.py`
  + undo), talk-to-twin chat mode, 12 new voice intents on real
  services, `/voice` quick prompts, mojibake repair in 10
  templates. 4 live Meta calls validated (chat/structured/tools/
  agent, ~$0.008). 40 tests in `tests/test_twin_flows.py`.
  FEATURE FREEZE after this checkpoint.
- `P8-final-audit` 2026-10-31: 400/400 non-E2E OK (~104s,
  SQLite-forced, sandboxed). E2E BLOCKED here (sandbox denies
  sockets: WinError 10013 in socketpair; needs unsandboxed
  runner — prior 9/9 NOT re-verified, do not cite as new
  evidence). Minimal fixes only, freeze held: (1) bare
  "Update my skills." now prompts for the change + collects
  the reply (was: unknown) via pending `update_collect`;
  (2) trailing punctuation no longer breaks update detection;
  (3) "Find me a ..." queries strip to the topic ("frontend
  internship", was: whole phrase as skill filter);
  (4) collect-state routing narrowed so a pending update is
  never swallowed. 6 regression tests added (collect,
  cancel, punctuation, find-me). Voice honesty re-verified:
  browser STT client-side (Chrome→Google disclosed on page),
  Meta transcription an honest refusing stub, /voice/command
  has login+CSRF+120/hr rate limit. No live Meta calls in
  this audit ($0.00). FEATURE FREEZE STILL IN EFFECT.
