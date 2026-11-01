# MIRA — Demo Playbook (one account, 3:15, verified)

One demo account for the entire recording: **Deepak**
(`deepak.demo@example.com` / `mira-deepak-demo`, fictional).
No logout/login switching. Verified 2026-11-01: 450/450 unit tests
pass, including single-account reachability and the full recruitment
flow on seeded data. Mock provider throughout; no AI credits spent.

## Setup (before recording)

```powershell
$env:MIRA_TEST_DATABASE_URL='sqlite'
D:\Others\Mira\.python\python.exe -m app.seed
D:\Others\Mira\.python\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`, log in as Deepak once to confirm, and
leave yourself logged in. Reset (dev machine only, server stopped):
delete `data/mira.db`, re-run seed. Never reset a shared database.

Seeded state for Deepak: twin Dee (public), 4 approved + 1 pending
memories, Karma's open Backend Developer post, taxi offerings, one
read-only public demo chat. Re-running seed creates nothing twice
and never resets passwords.

## The 3:15 recording (do not log out)

### 0:00–0:20 — The problem (landing page, no login needed)

Show `/` with hero "Your personal AI twin, working with others."
and the Create your twin / Watch twins talk actions. Then log in
as Deepak (or start logged in).

### 0:20–0:35 — Create a personal twin (`/me`)

Show the dashboard: twin Dee with persona and tone. Say this is
the result of creating a twin (profile + sharing permissions).
Twin creation itself lives at `/twin/new`, but this account
already has one — one twin per account, so do not demo a second
creation. If you say "create my twin" on the Voice page, Dee
honestly replies that a twin already exists.

### 0:35–1:00 — Teach the twin (`/memories`)

Show 4 approved + 1 pending seeded memories. Upload
`tests/fixtures/sample_export.json` (a ChatGPT-format file you
provide), then **Approve** one extracted item and optionally
**Reject** another. Say: MIRA never connects to ChatGPT accounts;
imports come only from files the user supplies, and nothing is
used until approved.

### 1:00–1:15 — What the twin can do (`/me`)

Scroll the journey (Create → Teach → Talk → Test → Activate →
Control) and the My twin / My conversations cards with their real
links: Edit twin, Memories, Voice, AI usage, Discover,
conversations. Claim only what these links do.

### 1:15–1:25 — Show the employer's job (Discover → posting)

**Discover** (Browse as Dee) → open **Backend Developer**. The
page names the employer distinctly: owner card "Twin of Karma",
"Have Dee contact Kay". You are logged in as Deepak viewing
Karma's public post — say so; do not imply ownership.

### 1:25–1:40 — Discover the opportunity (same page)

Point at **Match 85/100** (rank #1), explanation, and GraphQL
listed as missing. Say scores are rough fit indicators from
deterministic rules (skills 60, experience 20, location 20), not
predictions. This exact score is pinned by an automated test.

### 1:40–2:00 — Twin-to-twin collaboration (live chat)

Click **Have Dee contact Kay** → **Next twin message** 3–4 times.
Replies are deterministic mock simulations, labeled
`mock (simulated)` — call them a working prototype simulation,
never live AI. Optional 5-second beat: owner-note "What is your
phone number?", step once, Kay deflects (not authorized). Click
**Generate summary**: `RECOMMENDED for owner review` with
transparent criteria ending "The twin does not hire." Record
**Interested** as Deepak; show Decisions & audit. Both owners
keep final say; Karma's own decision button needs her login, so
do not claim her side on camera.

### 2:00–2:15 — Taxi service (Requests)

Requests → **New request** → "I need a taxi from GCIT to Thimphu
tomorrow at 9 AM." → review the parsed draft → Confirm with
budget 500 → shortlist shows **City Taxi in, Premium Taxi out**
(over budget, stated honestly). Stop at the shortlist for time;
contacting starts another twin chat exactly like 1:40. Do not
call this a completed booking.

### 2:15–2:30 — Update the twin (`/twin/edit`)

Change one field (e.g. availability note), Save, show it kept.
Say twins refine over time. Adding a second twin is not
supported (one per account; the form redirects to edit) — the
test suite locks this in, so present update-only honestly.

### 2:30–3:00 — Future scope (landing roadmap section)

Scroll `/` to **Prototype today, roadmap tomorrow**: left column
is working now, right column is planned (live providers on owner
keys, verified channel delivery, continuous voice, connectors).
Read one item from each column; stress the split.

### 3:00–3:15 — Conclusion (landing hero)

Back to the top: name, value proposition, journey. Closing line
as scripted. End recording while logged in.

## Fallbacks

- Match missing/wrong: re-seed, hard-refresh, confirm Browse-as
  is Dee. The 85 is test-pinned; if it differs, stop and report.
- Chat stalls: check the status badge (paused → Resume). The
  seeded demo chat caps at 8 turns; your live chat is separate.
- Slow app: free-tier cold start; wait 30 s, retry once.
- Mic fails: use the typed box; never claim the mic worked.
- Lost place: the read-only public demo chat under Chats always
  shows a finished twin conversation with zero clicks.

## Honest capability statement

Real implemented flows: registration/login, twin create/update
(one per account), memory import + approve/reject, deterministic
discovery/matching/shortlists, twin chats with pause/notes/audit/
decisions, recommendations, requests lifecycle, per-owner usage
log. Deterministic mock: all twin replies, scores, summaries.
Not implemented: live AI inference, continuous voice, second
twins, vendor message delivery, auto-hiring (by design).
Transcription runs in the browser (Chrome sends audio to Google).

## Pre-recording checklist

- [ ] Seed re-run shows `0 created` (known-clean state).
- [ ] Server up, `/healthz` ok, logged in as Deepak.
- [ ] Discover-as-Dee: Backend Developer 85/100, rank 1.
- [ ] No leftover live conversations from rehearsal (else reset).
- [ ] Fixture path ready: `tests/fixtures/sample_export.json`.
- [ ] `AI_PROVIDER=mock`; no keys or `.env` on screen.
- [ ] Recording captures browser only; passwords never typed
      visibly (log in before recording).

## Render/Neon demo setup (only with explicit approval)

Use a SEPARATE service + database for demo, never production
data. Set `MIRA_DEMO_MODE=1` (the only switch enabling demo
seeding/login), deploy, run `python -m app.seed` once in Render
Shell, verify each scene above, then rotate `SESSION_SECRET`,
wipe the demo DB, and clear the flag after the event.

---

# Speech script: what YOU say to MIRA (separate from narration)

**Delivery:** the mic captures ONE utterance per press (single-shot
recognition — verified in code), so speak in short takes: press
Start listening, say one chunk, wait for the reply, repeat. Each
"remember this" chunk becomes a **pending** memory; say
**"approve latest"** (or approve under Memories) to activate it.
Alternative: paste each chunk into the typed box and Send — same
result, more reliable on camera. Pace: conversational,
~140 words/minute; pause 2 seconds between takes.

**Front-loaded 60-second version** (say chunks 1–2 first; they carry
the demo if you trim to 15 seconds):

> **Chunk 1 (identity + goal, ~15 sec):** "Hi Dee, I'm Deepak, a
> backend developer from Thimphu. Remember this: I'm looking for a
> full-time hybrid backend role, and I want you to watch for good
> matches and prepare a short summary before I contact anyone."
>
> **Chunk 2 (skills, ~12 sec):** "Remember this: my key skills are
> Python, FastAPI, SQLite, and REST APIs, with three years of
> experience building small reliable web services."
>
> **Chunk 3 (preferences, ~12 sec):** "Remember this: I prefer clear
> next steps and short replies. Confirm with me before you contact
> any other twin or share anything new."
>
> **Chunk 4 (privacy, ~12 sec):** "Remember this: never share my
> phone number, salary expectations, or home address. If another
> twin asks for those, deflect and ask me first."
>
> **Chunk 5 (close, ~10 sec):** "That's everything for now. What do
> you remember about my job search?"

Chunk 5 makes Dee answer from approved facts only — the payoff
moment. Then approve the pending memories (voice "approve latest"
repeatedly, or one pass in Memories) so later answers can cite
"From my owner's notes". Pending items are never cited before
approval — say that if asked.

**15-second trim:** use Chunk 1 only, then "What do you remember
about my job search?" (approve Chunk 1's memory first, off-camera
or in Memories, or the answer will honestly say it has nothing
approved yet — which is itself a good privacy beat).
