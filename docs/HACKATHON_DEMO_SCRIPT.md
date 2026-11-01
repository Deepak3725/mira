# MIRA — Hackathon Demo Script (3–4 minutes)

Status of this script: drafted from source-code inspection on 2026-11-01
**while feature development is still running**. Scenes marked
**[PENDING FINAL VERIFICATION]** depend on code the other task is
editing (`app/voice/*`, `app/templates/voice.html`) and must be
rehearsed against the final code before recording. All other scenes
use code paths untouched by the in-flight work.

## Setup (before recording)

1. Fresh local database + demo data:
   `python -m app.seed` (idempotent; refuses production).
   Log in as `deepak.demo@example.com` (fictional demo account).
2. Have ready: `tests/fixtures/sample_export.json`
   (ChatGPT-format knowledge import), Chrome or Edge for voice.
3. Never show `.env`, passwords, tokens, or the `/usage` global view
   on screen. The seed passwords are fictional and demo-only.
4. Recording fallback for everything below: narrate over the page
   without clicking, using the Expected result as the talking point.

## Story

Deepak is a backend developer. His personal twin **Dee** answers from
Deepak's approved knowledge, meets Karma's hiring twin **Kay**, and
Deepak approves everything that matters.

---

### Scene 1 — MIRA vs personal twin (0:00–0:30)

- Screen: `/dashboard` (Twins).
- Action: show the twin cards (Dee, Kay, Pem, Tash).
- Expected: each twin shows owner name, persona, avatar; page makes
  clear every twin belongs to exactly one owner.
- Narration: "MIRA is the platform that creates, hosts, manages,
  secures, and connects personal AI twins. Each twin works only for
  its own owner. This is Deepak's twin, Dee."
- Fallback: static screenshot of `/dashboard`.

### Scene 2 — Twin profile + privacy (0:30–1:10)

- Screen: `/twin/edit` as Deepak.
- Action: scroll headline, skills (Python, FastAPI, SQLite, REST),
  then the **shareable-fields checkboxes**; point out phone, salary
  expectation, and home address exist in the profile but are NOT
  checked.
- Expected: checkboxes for headline/skills/experience/location/
  availability/portfolio/bio only; private fields stay unchecked.
- Narration: "Dee's owner decides field by field what other twins
  may ever see. Phone numbers, salary, and home address are stored
  but never shared — filtering happens on the server, not by
  hiding things in the browser."
- Fallback: narrate over the Discover page showing only public
  fields of another twin.

### Scene 3 — Approved knowledge (1:10–1:50)

- Screen: `/memories`.
- Action: upload `tests/fixtures/sample_export.json`, then click
  **Approve** on one candidate (optionally **Edit & approve**).
- Expected: candidate moves from pending to approved; pending items
  are never used for answers.
- Narration: "Dee learns from Deepak's own chat exports — ChatGPT,
  Claude, Gemini. Nothing becomes memory until Deepak approves it."
- Fallback: show the already-approved list; skip the upload.

### Scene 4 — Talk to the twin (1:50–2:20) **[PENDING FINAL VERIFICATION]**

- Screen: `/voice`.
- Action: type "do I like tea?" (or a question matching the just
  approved memory) in the typed box; leave **Speak replies aloud**
  checked. Optionally click the mic and speak it.
- Expected: reply appears in the transcript log citing the approved
  memory, and the browser speaks it aloud. Unapproved items are
  never cited; unknown topics get an honest "I don't know yet".
- Narration: "Deepak asks Dee what it knows. Every answer comes
  from approved memories only, with sources — and Dee says it
  out loud."
- Fallback: narrate over `/memories` approved list; say voice is
  in final testing. Do NOT claim voice worked if it did not.

### Scene 5 — Twin-to-twin meeting (2:20–2:50)

- Screen: `/conversations` → open the pre-seeded Deepak × Karma
  recruitment chat; click **Next twin message** once.
- Expected: a new turn appears with a `mock (simulated)` provider
  badge — MIRA labels simulated replies honestly. Then open
  `/discover` and point at Dee's **Match score** for Karma's
  Backend Developer post.
- Narration: "Dee meets Kay, the hiring twin, inside MIRA. Twins
  exchange only authorised fields, and every simulated reply is
  labelled as simulated. Discovery ranks posts by skill fit."
- Fallback: show the pre-seeded 6 turns without clicking Next;
  show Discover scores only.

### Scene 6 — Owner stays in control (2:50–3:20)

- Screen: `/channels` → connect **Mock (demo)** account → add a
  contact → **Save draft** → **Send now** (confirm=yes).
- Expected: message row shows `sent` with a `mock-…` receipt id.
  Then open `/usage`: per-user request/cost log. Mention **Revoke**
  wipes the account's tokens.
- Narration: "Nothing leaves MIRA without Deepak's confirmation,
  and nothing is called 'sent' without a delivery receipt. Every
  AI call is logged against his own usage, and access can be
  revoked in one click."
- Fallback: show the outbox list with statuses; skip the live send.

### Scene 7 — Close (3:20–3:40)

- Screen: `/dashboard`.
- Narration: "MIRA today: personal twins with approved knowledge,
  twin-to-twin meetings, skill matching, projects, messaging, and
  voice — all owner-controlled. Next: live provider calls on the
  owner's own keys, and a public pilot."
- No clicks; no fallback needed.

## Do-not-say list (honesty guardrails)

- Do not say any external message (WhatsApp/Messenger/Instagram/
  Telegram) was delivered — only Mock-channel receipts are real.
- Do not say Meta/OpenAI/Anthropic/Gemini answered live unless the
  final-verification report confirms a live call with receipts.
- Do not say voice audio stays private — Chrome sends recognition
  audio to Google; say "recognition runs in the browser".
- Do not say MIRA is production-ready or supports 1,000 users.
- Do not present the 352+9 green test run as proof of the latest
  code — it predates the in-flight edits.
