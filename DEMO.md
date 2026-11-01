# MIRA — 2-Minute Demo Script

Setup: `pip install -r requirements.txt && python -m app.seed`, then
`uvicorn app.main:app --host 127.0.0.1 --port 8000`.

Demo logins (password pattern `mira-<name>-demo`):

| Who | Email | Twin |
| --- | --- | --- |
| Deepak (job seeker) | deepak.demo@example.com | Dee |
| Karma (employer) | karma.demo@example.com | Kay |
| Pema (designer) | pema.demo@example.com | Pem |
| Tashi (freelancer) | tashi.demo@example.com | Tash |

## Taxi run (bot-to-bot, say this)

"Now the same network does services. Deepak tells his twin he needs a taxi
from GCIT to Thimphu tomorrow at 9 AM, max Nu. 500. Watch: the twin drafts
the requirements, Deepak confirms, the network ranks drivers without any AI
calls, and the twins talk using only approved facts. Deepak's Nu. 500 ceiling
is never shared — providers quote, his side filters, he decides."

1. Log in as Deepak → **Requests** → **New request** → paste the taxi need.
2. Review the parsed draft → Confirm → shortlist: City Taxi in, Premium out
   (over budget, stated honestly).
3. Contact Dor's twin → Auto-play → ask "What is your rate?" via owner note
   → Generate summary → record decision.
4. **AI usage** page: still $0.00, every call logged as mock.

## The story (say this)

"CVs are static. MIRA lets you talk to someone's AI twin instead. Every twin
shares only what its owner approved — watch the owners stay in control.
The journey is: create your twin, teach it, test it, activate it — the demo
cast just shows what's possible."

## Click path

1. **/** — "Personal AI twins that talk to each other."
2. **Twins** (`/dashboard`) — four demo twins, mascots, top matches each.
3. **Discover** (`/discover?twin_id=1`) — "Deepak's twin found Karma's Backend
   Developer: match 85/100, missing GraphQL — and here's exactly why."
4. **Log in as Deepak** → open the Backend Developer posting → **"Have Dee
   contact Kay"** → Auto-play 6 turns. "Two AIs exchanging only authorized
   facts. No phone numbers, no salary talk — ask and watch it deflect."
5. **Generate recommendation** — "Transparent criteria, and the verdict says
   the owners decide." Record a decision as Deepak.
6. **Chats** (`/conversations`) — show the pre-seeded public demo chat.
7. **Memories** (`/memories`, logged in) — import
   `tests/fixtures/sample_export.json` (a file you provide yourself — MIRA
   never connects to AI accounts), approve one item, show it surfacing
   in a later answer ("From my owner's notes").
8. **AI usage** (`/usage`) — "Every call counted, hard caps, $0 on mock."

## If asked…

- **Real AI?** "Mock brain today — deterministic and free. The Meta client is
  implemented against verified docs and budget-guarded, but stays off until
  billing is verified. Nothing here needs it."
- **My data?** "New twins are private by default. The privacy filter runs on
  the server before anything is shared or sent to any model."
- **Scale?** "Local demo architecture. SQLite + one process; honest limits
  are in the README."
