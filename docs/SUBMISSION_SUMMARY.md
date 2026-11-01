# MIRA — Hackathon Submission Summary

Drafted 2026-11-01 from source-code inspection **while feature
development is still running**. Items marked **[PENDING FINAL
VERIFICATION]** touch code the other task is editing and must be
rechecked (re-run the full suite + rehearse) before submission.
Nothing below claims a live external call succeeded — see
"Limitations".

## Product

**MIRA** — the platform that creates, hosts, manages, secures, and
connects personal AI twins. Each twin works for its own owner;
MIRA provides the environment and coordination.

## Problem

People's knowledge, skills, and opportunities are scattered across
chat apps, exports, and inboxes. Existing AI assistants answer from
a black box: no one can see what they were taught, what they are
allowed to share, or who approved an action taken "on your behalf".

## Solution and differentiator

A personal twin that (1) learns only from owner-approved knowledge,
(2) shares field-by-field only what the owner authorises, (3) meets
other twins inside MIRA instead of leaking data outward, and
(4) takes external actions only after explicit owner confirmation,
with receipts and audit trails. Mock/simulated behaviour is
labelled as such everywhere — never presented as live.

## Target users

Job seekers and employers (first demo story), independent
professionals, and — as a pilot audience — college students
organising skills, projects, and opportunities.

## Key capabilities actually implemented

All verified present in the repository on 2026-11-01:

- Accounts + personal twins: register/login (PBKDF2), twin
  create/edit, shareable-field allowlist, public/private listing.
- Knowledge import: authorised ChatGPT/Claude/Gemini JSON + ZIP
  exports (streaming, size/zip-bomb guards); owner review with
  approve / edit-and-approve / reject / delete. Meta AI has no
  machine export, so it is honestly listed as unsupported.
- Twin Q&A: answers from approved memories only, with source
  citations and confidence; honest "don't know" fallback;
  read-only by construction. Reached via the voice route
  **[PENDING FINAL VERIFICATION]** (route code mid-edit).
- Voice + typed interface: `/voice` page with mic (Web Speech
  API), typed box, transcript log, spoken replies, quick prompts
  **[PENDING FINAL VERIFICATION]** (template + handlers mid-edit).
- Twin-to-twin chat: purpose-driven conversations, turn limits,
  pause/resume/close, auto-play; pre-seeded Deepak × Karma demo.
- Discovery: opportunity board + post-an-opportunity + skill-match
  scores computed from authorised fields only.
- Requests: need parsing, owner review/confirm, lifecycle.
- Projects: spec draft, task DAG, mock-agent execution, versioned
  file workspace, approval-gated jailed code execution with
  timeouts/output caps, review loop, final report, pause/resume.
- Messaging: channel framework (Telegram, WhatsApp, Messenger,
  Instagram, Mock) with connect/activate, contacts, drafts,
  confirm-before-send, scheduling, persistent outbox with explicit
  triggers, retries, idempotency, per-account permissions, revoke
  (wipes tokens), full audit. Only the Mock channel can deliver
  without external credentials.
- Multi-provider AI layer: adapters for Meta, OpenAI, Anthropic,
  Gemini + mock; capability routing; budgets; retry/backoff;
  per-call provider/model/latency/cost accounting. Unpriced
  vendors are refused before any network call.
- Usage and audit: per-user `/usage` cost log; audit events for
  twin chats, messages, projects, voice turns, twin changes.

## Technical architecture

Python + FastAPI, server-rendered Jinja pages, SQLite (default)
or PostgreSQL (Neon) via a dual-driver layer with idempotent
migrations (schema v12). Packages: `app/routes/` (HTTP),
`app/ai/` (provider orchestration), `app/importers/`,
`app/projects/`, `app/channels/`, `app/voice/`, `app/twin_mind.py`,
plus `app/twin_setup.py` / `app/twin_update_nl.py`
**[NEW, PENDING FINAL VERIFICATION]**. Tests: stdlib unittest,
last green checkpoint 352 non-E2E + 9 E2E (2026-11-01, SQLite)
— predates in-flight edits; must be re-run before submission.

## Privacy, approval, and safety (genuinely present)

- Server-side field allowlist: nothing shared unless authorised;
  private profile fields (phone, salary, addresses in seed data
  marked private) never leave the server.
- Owner review before imported knowledge activates; per-memory
  approve/reject/delete.
- Show-before-send + explicit confirmation for messages;
  delivery claimed only with provider receipts.
- Approval gates for code execution; argv allowlist, temp-jail
  cwd, secret-scrubbed env, timeouts, output caps. No container
  isolation — documented, not claimed.
- Session CSRF on every POST; logout revokes the session
  server-side; rate limits on writes; upload MIME/size checks;
  secret-redacting JSON logs; per-user usage scoping.
- Twins cannot hire, buy, pay, sign, or commit — approvals stay
  with the owner.

## Current limitations and unverified capabilities

- **No live external call is claimed.** Contributors' keys are not
  configured for judges; Meta promo credit (~$148 owner-reported,
  expiry 2026-11-04 owner-reported) may be used by the other task
  for validation — recheck spend/receipts before submitting.
- Voice speech-to-text needs Chrome/Edge and sends audio to
  Google; it is free but not fully private. No server-side
  transcription yet (Meta Transcribe is an honest stub).
- Real WhatsApp/Messenger/Instagram/Telegram delivery needs the
  owner's own apps, tokens, and platform approvals — not present.
- Single-worker design; rate limits, voice pending state, and
  half-built setups are in-memory (lost on restart).
- No user data export/download or account deletion yet; no
  background worker (outbox needs explicit triggers); deps
  unpinned; no load testing — **not production-certified, no
  1,000-user claim**.
- Conversational twin creation/update and new voice intents are
  implemented but **[PENDING FINAL VERIFICATION]** (tests not yet
  green on final code).

## Future roadmap (not current functionality)

Live provider calls on owner keys; server-side transcription;
background job worker; persistent voice/setup sessions; data
export + account deletion; containerised code execution; load
testing toward a student pilot; public hosting when funded.
