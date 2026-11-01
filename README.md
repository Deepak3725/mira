# MIRA — Personal AI Digital-Twin Network

MIRA is a network of personal AI twins. Each twin represents its owner —
identity, skills, experience, preferences — and can talk to other twins,
exchange explicitly authorized information, and coordinate tasks. Owners watch
everything and keep every final decision. Recruitment is the first demo use
case, not the whole product.

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
  explanations. Zero AI cost.
- **Twin-to-twin chat** — reusable conversation engine: turns, pause/resume,
  owner notes, withdrawal via close, audit trail, turn limits, per-side owner
  decisions. Deterministic mock brain included; real Meta inference is an
  optional, budget-guarded upgrade (currently OFF).
- **Recruitment demo** — discover → twin contact → exchange → transparent
  recommendation → owners decide. Twins never hire.
- **Knowledge import** — upload ChatGPT `conversations.json`, review extracted
  candidates, approve/edit/reject/delete. Retrieval is local keyword search;
  no embeddings, no vector DB.
- **Cost controls** — usage log (tokens, model, $ estimate), monthly /
  per-user-daily / per-conversation caps, graceful mock fallback with a
  visible notice when inference is unavailable or exhausted.
- **Privacy by default** — discovery, matching, and recommendations compute
  from authorized public fields only; new twins are private unless listed;
  private twins can't be contacted; non-owners get 404s, not errors.
- **Bot-to-bot requests** — post a need in words, confirm the parsed draft,
  get a ranked provider shortlist (price 40, coverage 30, availability 15,
  tags 15), contact twins idempotently, chat with owner conditions enforced
  (budget filter, auto-answer and per-turn approval gates). Seeker budget
  ceilings are never shared with providers. Twins cannot pay or commit.

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
python -m app.seed
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). See [DEMO.md](DEMO.md)
for the 2-minute demo script and demo logins.

Run tests (stdlib `unittest`; the HTTP E2E file needs the test extras):

```powershell
pip install -r requirements-test.txt
python -m unittest discover -s tests
```

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
| `MIRA_TEST_DATABASE_URL` | _(empty)_ | Suite runs against this Postgres (staging branch) when set. |

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
  re-verify free-tier limits at deploy time. Server binds and E2E browser
  flows must be verified outside sandboxed runners.

## Project layout

```
app/            # FastAPI app: routes, engine, providers, import pipeline
tests/          # unittest suites per phase + sample export fixture
data/           # SQLite file (created at runtime, git-ignored)
DEMO.md         # demo script
```
