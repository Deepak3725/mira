# MIRA Demo Guide

MIRA is a personal AI twin network: every user gets a twin that
knows what they approved, finds things for them, and coordinates
with other twins. Owners keep every decision. Presenters follow
[DEMO_PLAYBOOK.md](DEMO_PLAYBOOK.md); this file is the setup and
tour reference.

## Setup

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r requirements-test.txt   # for the test suites
copy .env.example .env
```

Seed an explicit demo database (idempotent: safe to rerun; never wipes).
`--clean` seeds users, twins, profiles, and memories only — no
prearranged needs, offers, matches, notifications, or conversations:

```powershell
$env:AI_PROVIDER='mock'
.\.venv\Scripts\python.exe -m app.seed --db data/demo.db --clean
```

Run the server against that same database (mock brain: $0, deterministic):

```powershell
$env:AI_PROVIDER='mock'; $env:DATABASE_PATH='data/demo.db'
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Fresh state
between takes: stop the server, `Remove-Item data/demo.db*`,
reseed with `--clean`, restart.

## Demo logins (password pattern `mira-<name>-demo`)

| Who (role) | Email | Twin |
| --- | --- | --- |
| Deepak (full-stack developer) | deepak.demo@example.com | Dee |
| Karma (studio lead) | karma.demo@example.com | Kay |
| Pema (designer) | pema.demo@example.com | Pem |
| Tashi (freelancer) | tashi.demo@example.com | Tash |
| Choden (junior analyst) | choden.demo@example.com | Cho |
| Tshering (counsellor, strict privacy) | tshering.demo@example.com | Tshe |
| Wangchuk (researcher) | wangchuk.demo@example.com | Wang |
| Dema (illustrator) | dema.demo@example.com | Dem |
| Dorji (city taxi) | dorji.demo@example.com | Dor |
| Sonam (long-trip taxi) | sonam.demo@example.com | Son |

Seeded demo state: 10 users + twins with populated profiles and
interests, approved + pending memories. No requests, offers,
matches, notifications, or conversations until you create them.

## The product in five pages

- **Find Something** (`/find`) — talk to your twin in words. It
  searches, asks one targeted question only when a real gap blocks
  discovery, shows genuine result cards, and honestly reports when
  nothing exists yet. The search persists and keeps matching.
- **Post Something** (`/post`) — describe a need or offer in one
  box. The twin infers the type and details, you review and
  publish, publishing triggers real discovery.
- **My Conversation** (`/conversations`) — real persisted twin
  coordination, per-owner decisions, pause/resume, notes.
- **Notification** (`/notifications`) — real events linked to
  their search, post, or chat.
- **Twins** (`/dashboard`) — create and manage your twin, review
  sharing permissions, browse public twins.

Old form flows (`/requests/new`, `/opportunities/new`,
`/discover`) still work but are no longer the product surface.

## Core moves

1. **Find:** Deepak opens Find Something and types
   `I'm looking for a full-stack developer job in Bhutan.` The twin
   uses his profile and searches; with no offer yet it says so.
2. **Post:** Karma opens Post Something and types
   `I'm hiring a full-stack developer for my team...` Reviews the
   inferred offer (Type Offer, skills, location) and publishes.
   Matching triggers instantly.
3. **Match:** Deepak's Notification shows New match; his chat shows
   the posting card with a relevance reason.
4. **Decide:** Each owner accepts independently from the search or
   posting view; acceptance starts a coordination chat.
5. **Coordinate:** My Conversation shows the real chat. Auto-play
   exchanges authorized facts; Record decision captures each
   owner's Interested/Not interested separately.
6. **Beyond jobs:** the same two pages handle taxis
   (`I need a taxi from Thimphu to Dagana tomorrow. I need four
   seats.`), piano lessons, rentals, and custom needs — one engine,
   no special cases.
7. **Import:** Twins → Memories accepts a self-exported
   `conversations.json` (try `tests/fixtures/sample_export.json`),
   queues items for review, and only approved knowledge is used.

## Notes

- Twins publish and contact only with owner authorization; budgets
  are never shared with providers; private fields stay private.
- Match reasons never contain prices or budgets; decisions are
  always per-owner.
- Mock provider: every call is logged on the Usage page at $0.
