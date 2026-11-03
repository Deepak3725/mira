# MIRA — Personal AI Digital-Twin Network

MIRA is a network of personal AI twins. Each twin represents its owner —
identity, skills, experience, preferences — and can talk to other twins,
exchange explicitly authorized information, and coordinate tasks. Owners watch
everything and keep every final decision. Opportunity coordination is the
first demo use case, not the whole product.

MIRA is not a recruitment screener: it never ranks applicants, scores
suitability, assesses interviews, or issues hiring verdicts. Twins help
their own owners discover opportunities and coordinate; assessment of
other people is deliberately absent (see Compliance below).

## Your twin journey

1. **Create** your twin — profile plus per-field sharing permissions.
2. **Teach** it — import your own exports, approve each memory.
3. **Test** it — discover matches, start a twin chat, read the summary.
4. **Activate** it — list it publicly so it can represent you.
5. **Control** it — set conditions (budgets, auto-answer rules, per-turn
   approval), pause any chat, record every decision yourself.

The four fictional demo twins (Deepak, Karma, Pema, Tashi) are examples of
what personal twins can do — the product is your twin, not the demo cast.

## What MIRA can do (MVP)

- **Personal twins** — register, create one twin, edit profile, tick exactly
  which fields it may share, delete it anytime. New twins are private unless
  listed publicly. Four fictional demo twins included (Deepak, Karma, Pema,
  Tashi) with original programmatic SVG mascots.
- **Discovery + deterministic matching** — skill/experience/location scoring
  (0–100 rough fit indicator, never a success prediction), filters, and
  explanations. Zero AI cost. Relevance is computed for your own twin
  only — MIRA never scores other people's twins.
- **Twin-to-twin chat** — reusable conversation engine: turns, pause/resume,
  owner notes, withdrawal via close, audit trail, turn limits, per-side owner
  decisions. Deterministic mock brain included; real Meta inference is an
  optional, budget-guarded upgrade (verified with small live batches, OFF
  by default).
- **Opportunity coordination demo** — discover → twin contact → exchange →
  owners decide. Twins coordinate; they never assess applicants.
- **Knowledge import** — upload ChatGPT `conversations.json`, review extracted
  candidates, approve/edit/reject/delete. Retrieval is local keyword search;
  no embeddings, no vector DB.
- **Cost controls** — usage log (tokens, model, $ estimate), monthly /
  per-user-daily / per-conversation caps, graceful mock fallback with a
  visible notice when inference is unavailable or exhausted.
- **Privacy by default** — discovery and matching compute from authorized
  public fields only; new twins are private unless listed; private twins
  can't be contacted; non-owners get 404s, not errors. The twin editor
  previews exactly what other twins can see.
- **Bot-to-bot requests** — post a need in words, confirm the parsed draft,
  get matching providers with the matching facts shown (no scores, no
  verdicts), contact twins idempotently, chat with owner conditions
  enforced (budget filter, auto-answer and per-turn approval gates).
  Seeker budget ceilings are never shared with providers. Twins cannot
  pay or commit.
- **Find Something (universal posts)** — one workflow for jobs, taxis,
  services, tutors, collaborators, and arbitrary needs: describe it in
  words, preview, confirm, edit, or withdraw. Discovery
  runs both directions: seekers find offers, and helpers discover open
  needs they could answer. Categories are labels, never gates.
- **Two-sided coordination** — every twin chat appears in both owners'
  Requests pages with per-owner interest shown separately. Each owner
  records interested/not-interested independently; recording interest
  is never a commitment.

## Prerequisites (Windows)

- Python 3.11+ from [python.org](https://www.python.org/downloads/) (check
  "Add python.exe to PATH"). SQLite ships with Python.
- No paid services. No credit card. No cloud account.

## Setup & run

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Seed an explicit demo database (idempotent: safe to rerun; never wipes).
The `--db` target is printed before anything is written:

```powershell
$env:AI_PROVIDER='mock'
.\.venv\Scripts\python.exe -m app.seed --db data/demo.db
```

Run the server against that same database (mock brain: $0, deterministic):

```powershell
$env:AI_PROVIDER='mock'; $env:DATABASE_PATH='data/demo.db'
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). See [DEMO.md](DEMO.md)
for the demo walkthrough, demo logins, and how to reset the demo database
(delete `data/demo.db*` explicitly, then reseed — the seeder itself never
deletes anything). Presenters follow
[DEMO_PLAYBOOK.md](DEMO_PLAYBOOK.md), the verified step-by-step demo
script with preflight, recovery, and cleanup safety.

Run tests (stdlib `unittest`; the HTTP E2E file needs the test extras).
Use exactly this form: `-t .` (or `tests.*` module paths) is required so the
`tests/__init__.py` safety bootstrap loads and forces mock/offline mode:

```powershell
pip install -r requirements-test.txt
$env:MIRA_TEST_DATABASE_URL='sqlite'; $env:DATABASE_URL=''
python -m unittest discover -s tests -t .
```

Do NOT use `discover -s tests` without `-t .` or run test files directly as
scripts: both bypass the safety bootstrap and ambient provider configuration
would apply. The HTTP E2E module drives the app fully in-process through a
socket-free ASGI transport, so all tests (632, including 10 E2E, 14
full-stack journey tests, and the 11-journey synthetic-user suite) pass
even in restricted sandboxes.

## Configuration (.env)

| Variable | Default | Purpose |
| --- | --- | --- |
| `MODEL_API_KEY` | _(empty)_ | Meta Model API key. Unused unless real inference is enabled. Never commit. |
| `SESSION_SECRET` | dev default | HMAC key for login cookies. Set a long random value; the default is local-demo only. |
| `AI_PROVIDER` | `mock` | `mock` ($0, default) or `meta` (only after the checks below). |
| `META_MODEL` | _(empty)_ | Verified Standard-mode model id, e.g. `muse-spark-1.3`. Empty = real calls refused. |
| `BUDGET_MONTHLY_USD` | `75.0` | Hard monthly cap. Inference stops at the cap. |
| `BUDGET_PER_CONVERSATION_USD` | `1.0` | Hard per-conversation cap. |
| `BUDGET_PER_USER_DAILY_USD` | `5.0` | Hard per-user daily cap. |
| `APP_ENV` | `dev` | `production` enables secure cookies, demo blocks, secret checks. |
| `DATABASE_URL` | _(empty)_ | Neon Postgres string for deployment. Empty = local SQLite. |
| `DATABASE_PATH` | `data/mira.db` | SQLite file used when `DATABASE_URL` is empty. |
| `MIRA_TEST_DATABASE_URL` | _(empty)_ | Suite runs against this Postgres (staging branch) when set. Test-only: the server always ignores it. |
| `MIRA_DEMO_MODE` | `false` | `1` allows demo seeding/logins on an isolated demo service. Never on a production user database. |

Public deployment (Render + Neon, $0) is documented separately in
[DEPLOY.md](DEPLOY.md).

## Enabling real Meta inference (optional, owner only)

Verified from official docs (dev.meta.ai, 2026-10-30): `POST
https://api.meta.ai/v1/chat/completions`, Bearer key, OpenAI-compatible
shape. `muse-spark-1.3` Standard: $1.25 in / $4.25 out per 1M tokens, content
not used for training. Contributor-mode ids are refused by the app.

Before setting `AI_PROVIDER=meta`, the owner must personally verify in the
Meta dashboard: credit balance and hackathon eligibility, whether billing
activation or a payment method is required, and what happens past the credit
balance. If any of that is unclear, stay on `mock` — the demo is complete
without real inference. Never paste keys into chat; put them in `.env` only.

## Architecture

```
Browser → FastAPI (app/routes) → engine (conversations, matching, memories)
        → providers (mock default, meta optional) → budget guard → SQLite
```

- DB access is plain SQL in `app/db.py` with a shared SQLite/PostgreSQL
  abstraction; production runs on Neon Postgres via `DATABASE_URL`.
- Messaging channels (Telegram/WhatsApp/Messenger/Instagram/Mock) exist
  with drafts, confirmations, and a mock channel for offline demos; only
  Mock delivery is verified end to end — vendor delivery needs a live
  token and a real handshake test before any claim.

## Compliance (no applicant screening)

MIRA deliberately contains no automated recruitment screening: no candidate
ranking, no suitability scores of other people, no interview assessment, no
hiring verdicts, and no "job" hiring-request category. Relevance scores are
computed for the viewer's own twin only; provider lists show matching facts
without scores; close-outs append no assessments. Compliance is enforced by
route-level tests (`TestJourneyCompliance`) and verdict-absence tests. This
is the project's own interpretation of the hackathon's AI Job Application
Screener prohibition — no official approval is claimed.

## Honest limitations

- **Auth**: PBKDF2 passwords, HMAC-signed sessions (revoked on logout),
  session-bound CSRF tokens, and per-IP rate limits are implemented and
  tested. Still missing: email verification, password reset, and brute-force
  lockout beyond in-memory rate limits (which reset on restart, are
  per-process, and don't cover every endpoint). Demo accounts use published
  passwords and are blocked in production unless `MIRA_DEMO_MODE=1` on an
  isolated demo service. Do not run open public registration on the
  internet as-is.
- **Database**: SQLite locally (one writer; fine for demo), Neon Postgres
  in production. Channel tokens are stored as plaintext JSON in the
  database: acceptable only with strict database access control; encrypt
  at rest before storing real vendor tokens.
- **Mock brain**: template replies from authorized facts — good for demo
  structure, not real reasoning. Clearly labeled `mock` in the UI.
- **Import**: ChatGPT/Claude/Gemini exports (JSON/ZIP, size-capped) plus a
  tiny generic JSON shape. No live sync with any AI account, ever.
- **Hosting**: Render Free + Neon Free blueprint in `render.yaml`;
  re-verify free-tier limits at deploy time. E2E HTTP flows are verified
  in-process; real-browser rendering still needs a manual check.
- **Deletion**: twin deletion is supported; user-account deletion has no
  route yet (documented gap).

## Project layout

```
app/            # FastAPI app: routes, engine, providers, import pipeline
tests/          # unittest suites per phase + sample export fixture
data/           # SQLite file (created at runtime, git-ignored)
DEMO.md         # demo script
DEMO_PLAYBOOK.md # verified presenter playbook (preflight + 3:15 script)
```
