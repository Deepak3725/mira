# MIRA — Demo Playbook

The canonical presenter guide. Every step was executed against the
real application before it was written here. Tags on each step:

- **LIVE** — do this on camera.
- **PREP** — do this before recording.
- **FALLBACK** — only if a genuine dependency fails.

Companion docs: [DEMO.md](DEMO.md) (setup + tour),
[README.md](README.md) (product + test commands),
[docs/MANUAL_BROWSER_CHECKLIST.md](docs/MANUAL_BROWSER_CHECKLIST.md)
(full acceptance checklist).

## Core idea (say this first)

Every person gets a personal AI twin. Your twin carries only
information you approved, finds things for you when you ask in
words, publishes requests or offers only with your authorization,
and coordinates with other people's twins — while you keep every
decision.

MIRA is one universal **Find Something** system. Jobs, taxis,
tutoring, rentals, and custom requests all run through the same
conversation, matching, notification, and two-sided decisions.
Nothing here ranks applicants, scores suitability, assesses
interviews, or issues hiring verdicts.

## Setup and preflight checklist (PREP)

Run from the project root in Windows PowerShell. Uses the mock
brain: $0, deterministic, no network.

```powershell
$env:AI_PROVIDER='mock'
Remove-Item data/demo.db* -ErrorAction SilentlyContinue
.\.python\python.exe -m app.seed --db data/demo.db --clean
$env:AI_PROVIDER='mock'; $env:DATABASE_PATH='data/demo.db'
.\.python\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Then open http://127.0.0.1:8000.

1. The seeder prints its target, is idempotent, and never deletes.
   `--clean` seeds the ten users, twins, profiles, and memories —
   and zero needs, offers, matches, notifications, or conversations.
2. Confirm the public landing loads with no one logged in.
3. Log in as Deepak (`deepak.demo@example.com` /
   `mira-deepak-demo`); confirm Find Something shows "No messages
   yet" and Notification is empty. Log out again before recording.
4. Open two browser profiles: log in as **Deepak** in one and
   **Karma** in the other (`karma.demo@example.com` /
   `mira-karma-demo`). Leave both on Find Something.
5. Confirm the nav reads exactly: Post Something, Find Something,
   My Conversation, Notification, Twins.
6. Confirm mock mode: the shell shows `AI_PROVIDER=mock`, and the
   Usage page shows $0 after your actions.
7. Fresh start between takes: stop the server, run
   `Remove-Item data/demo.db*` (this deletes ONLY the named demo
   database), reseed with `--clean`, restart, re-log both profiles.

If login fails: reseed and retry — do not edit the database by
hand. If a match does not appear: refresh, check Notification on
both sides, and see Recovery below.

## Primary 3:15 script — Deepak finds a developer opportunity

One continuous story on two pre-logged profiles. Do not show login.

| Time | Say | Click / type | Expected result | Proves |
| --- | --- | --- | --- | --- |
| 0:00–0:20 | "Inboxes are chaos and CVs are static. MIRA gives everyone a personal AI twin that knows what they approved, finds things for them, and coordinates with other twins — humans decide." | On Find Something (Deepak). | "What are you looking for?" with an empty chat. | Problem |
| 0:20–0:50 | "Deepak just tells his twin what he wants — in words, no forms." | Type `I'm looking for a full-stack developer job in Bhutan. Preferably something relevant to my skills.` → Send. | Twin replies it is searching, cites Deepak's profile skills, and honestly reports nothing suitable yet with a keep-looking date. | Natural-language persistent search |
| 0:50–1:15 | "Nothing waits for a scheduler. Watch Karma publish a real posting — in words too." | Karma profile: Post Something → type `I'm hiring a full-stack developer for my team. I need someone with JavaScript, Node.js, frontend, and backend skills. The role is based in Bhutan.` → Review → Publish. | Review shows Type Offer with parsed skills and location; Karma lands on the new posting. | Owner-authorized NL publishing |
| 1:15–1:40 | "Saving the post triggered matching instantly — Deepak already has a notification." | Deepak profile: Notification → open the New match item → open the search. | The chat shows the posting card with a relevance reason; Karma's Requests page lists it under "Interest in my posts". | Event-driven matching, both sides notified |
| 1:40–2:05 | "Deepak accepts his side. Notice Karma has her own decision — nobody is committed yet." | Deepak: Accept → a coordination chat starts. Karma profile: Requests → open the interest → I'm interested. | Search shows "Accepted 1 of 1"; both sides accepted independently. | Two-sided decisions, no implied commitment |
| 2:05–2:25 | "The twins exchange authorized facts only — private fields stay home." | Open the chat → Auto-play 6 turns. | Six twin messages, each grounded in approved fields. | Twin-to-twin coordination |
| 2:25–2:45 | "Each owner records interest separately. Interest is never a commitment." | Both profiles: Record decision → Interested. | Decisions appear per owner on the chat. | Human final decisions |
| 2:45–3:00 | (Talk.) "Twins that carry your preferences anywhere and coordinate services for you — but always ask before committing. Today: conversation, discovery, coordination, import, controls." | — | — | Vision vs implemented |
| 3:00–3:15 | "Personal twins, owner control, no screening. That's MIRA." | Usage page. | Every call logged, $0 on mock. | Cost transparency |

## Secondary scenario — piano teacher (LIVE, extension)

Proves matching is not hardcoded to jobs. Tested with these exact
words:

1. **Dema** (profile 1): Find Something → type
   `I want to find a piano teacher who can teach beginners on weekends.`
   → Send. The twin searches; with no offer yet it says so honestly.
2. **Wangchuk** (profile 2): Post Something → type
   `I can provide piano lessons on weekends for beginners.`
   → Review (Type Offer) → Publish.
3. Dema's chat shows the piano offer despite different wording;
   Wangchuk's Notification shows the interest. Accept either side
   and confirm the other side still shows its own pending decision.

If the 3:15 script runs long, run this instead of the taxi
extension. Same workflow as jobs — nothing special-cased.

## Optional extensions (LIVE, only where verified)

### Taxi with four seats

1. **Tashi**: Find Something →
   `I need a taxi from Thimphu to Dagana tomorrow. I need four seats.`
   → Send. No route question is asked; the search shows
   "Accepted 0 of 4" (quantity parsed, never stored as a skill).
2. **Dorji**: Post Something →
   `I have a taxi available from Thimphu to Dagana tomorrow.`
   → Review (Type Offer) → Publish.
3. Tashi's chat gains the match; accepting one shows
   "Accepted 1 of 4" — never "fulfilled". A rejection restores the
   remaining count.

### Short question flow

1. Any account: Find Something → `Find me a taxi.` → Send.
2. The twin asks one targeted question: where the trip starts and
   ends. Reply `From Thimphu to Dagana.` — the twin merges it into
   the same search (no duplicate) and keeps looking.

### ChatGPT knowledge import

1. **Wangchuk**: Twins → Memories (or profile → Memories). The page
   states the supported format (a `conversations.json` export you
   produce yourself), size limits, and that only your own messages
   are extracted.
2. Upload `tests/fixtures/sample_export.json` → **Import for
   review** → the review queue lists pending items.
3. Edit one item, **Save & approve**; **Reject** another. Approved
   knowledge becomes usable by the twin; rejected items never are.

### Deadline expiry (test-verified, not staged live)

The chat defaults searches to one week, so expiry is not staged
inside a recording. Behavior is covered by the automated suite
(`tests/test_matcher_flows.py::TestScenarioF_Expiry`): the search
stays active before the deadline, expires after it, the owner is
notified, history and matches are preserved, and matching resumes
only after the owner authorizes an extension.

## Recording preparation and recovery plan

**Before recording:** seed `--clean` fresh; start the server;
pre-log Deepak + Karma; leave both profiles on Find Something;
close unrelated tabs; keep this playbook open off-camera.

**Order:** primary script first, piano second, one extension only
if time allows. Never run two extensions in one take.

**Profile switching:** use two separate browser profiles — never
log out/in on camera. Verify each result on screen before moving
on (reply text, card, counter, notification) rather than trusting
clicks.

**Reset between takes:** stop server →
`Remove-Item data/demo.db*` → reseed `--clean` → restart → re-log
both profiles.

**Recovery:**

- Match missing: refresh both profiles; open Notification on both
  sides; open the search to see its live state. If still missing,
  re-send the Find message with one more detail (e.g. add the
  place) and re-check — then describe honestly what happened.
- Server error on camera: stop, reseed fresh, restart, resume from
  the last verified step. Never edit the database live.
- No external dependency exists in this demo (mock brain, local
  SQLite), so there is no network fallback to prepare.

## Demo data and cleanup safety

- **Demo accounts:** the ten seeded `*.demo@example.com` logins
  with populated profiles and twins. Reseed restores them.
- **Per-take activity:** created live during recording; wiped by
  the documented `Remove-Item data/demo.db*` + reseed cycle.
- Never delete all users, clear an unknown database, or run
  destructive commands against `data/mira.db` (the persistent
  local database) or any production data.
- This playbook contains no secrets: demo passwords are
  intentional fictional credentials from the safe seed procedure.

## After the demo: push + deploy (owner runs)

Only after the manual browser checklist passes on localhost.

### 1. Push to GitHub

```powershell
git status --porcelain
git add <intended files>   # or: git add -A after reviewing the list
git commit -m "MIRA twin-chat redesign + demo verification"
git push origin main
```

Check before pushing: no `data/*.db`, no `.env`, no API keys or
passwords in the diff (`.gitignore` already excludes them; the two
untracked `MISSION_*.md` logs are optional work notes — include or
leave them out as you prefer). Confirm on GitHub that the file list
shows source and docs only.

### 2. Deploy to Render

Full detail in [DEPLOY.md](DEPLOY.md); the short path:

1. **Neon:** create project `mira-prod` plus a `staging` branch.
   Copy both connection strings (they end with `?sslmode=require`).
2. **Render:** New → Blueprint → connect this repo. Plan must show
   **free**; never link a payment method.
3. When prompted: paste the Neon **prod** string into
   `DATABASE_URL`, leave `MODEL_API_KEY` empty (mock brain, $0),
   keep `SESSION_SECRET` generated.
4. Deploy, then open `/healthz` and register a fresh account.
   Demo `@example.com` logins are rejected in production unless
   `MIRA_DEMO_MODE=1` is set — only ever set that on an isolated
   demo service with a demo database.
5. Expect ~1 minute cold start after 15 idle minutes (Render free
   sleeps; Neon free scales to zero). Production uses Postgres
   only — Render's disk is ephemeral, so local SQLite files never
   persist there.

Secrets live only in Render environment variables, never in Git.

## Verification statement

The author executed the primary script's mechanics (Deepak's chat
message exactly as worded, Karma's posting exactly as worded,
honest empty state, immediate match on publish, both
notifications, both accepts, coordination chat) plus the piano
pair, the taxi quantity flow, and the question-answer merge over
real HTTP routes with sessions, CSRF, and database state: PASS in
all cases (see `tests/test_twin_chat.py`, 24 tests). Expiry is
covered by `TestScenarioF_Expiry`, not staged live. Real-browser
driving could not run in the build sandbox (no browser driver
installed, Chrome cannot launch, loopback `listen()` forbidden —
three independent probes); the owner runs
[docs/MANUAL_BROWSER_CHECKLIST.md](docs/MANUAL_BROWSER_CHECKLIST.md)
as the browser gate on real hardware.
