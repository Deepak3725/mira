# MIRA Product Gap Analysis (2026-11-02 audit)

Evidence from direct code inspection. Every claim cites the file inspected.

## 1. "Deepak is logged in immediately"

**No auto-login code exists** (`app/` has no auto/demo-login path;
`GET /` in `app/routes/pages.py:124` only reads the session cookie).
Root causes of the experience:

- `SESSION_TTL_SECONDS = 7 * 24 * 3600` (`app/auth.py:23`): a login
  persists a week, so the owner's browser stayed logged in across visits.
- `index.html` ignores `me`: logged-in visitors see the identical
  marketing page, with only the nav changing — a confusing half-public state.

Fix: logged-in `/` redirects into the app (`/find`); logged-out `/`
shows a slim public landing. No session behavior change needed.

## 2. Discovery is a listing website by design

`/discover` (`app/routes/pages.py` + `app/templates/discover.html`)
is a keyword/filter listing page with ranked cards. The request flow
(`/requests/new` → review) forces manual fields: budget, origin,
destination, when, skills, category, duration — with the warning "No
structured details found; please fill them in below."
(`app/routes/requests.py`, `app/templates/request_review.html`).
There is no user-facing "talk to your twin to find things" page
(Voice has typed commands, but it is a command box, not discovery).

Fix: new conversational `/find` (twin chat) as the primary
experience; new natural-language `/post`; delink `/discover` from
navigation (route kept working for regression safety).

## 3. "When" contains "am"

Root cause in `app/request_parse.py:49-51,120-137`: `_WHEN_WORDS`
includes bare `"am"`/`"pm"` matched with `\b` word boundaries, so any
"I am ..." sentence sets `when_text="am"`. The review form then
displays it as the parsed time.

Fix: bare am/pm only count when attached to a clock time
(`9 AM`, `9:30pm`); otherwise dropped. Pinned case
"tomorrow at 9 AM" → "tomorrow at 09:00" (`tests/test_b2b_phase1.py:72`)
is preserved. Related hardening: skill extractor must skip
number-words/quantity nouns ("I need four seats" must not yield
skill "four seats").

## 4. Skills/profile not reused by the UX

Engine-side reuse exists: `matcher._consider_pair`
(`app/matcher.py:290-304`) merges the searcher's approved-shared
profile skills into the search. But the UI makes the user re-enter
skills in the review form, and `interpret_need` (`app/intent.py`)
never sees the profile. Seed profiles are also thin for the demo:
Deepak lacks JavaScript/Node/frontend/backend skills his scenario
needs; some twins have minimal context.

Fix: chat flow loads the twin profile automatically (full profile
for understanding the owner; only authorized fields ever shared or
matched externally); enrich seed profiles; add `interests` to the
twin profile form.

## 5. Prearranged demo activity

`app/seed.py` creates: 4 opportunities (`seed_demo_opportunities`),
2 driver users + 2 taxi offerings (`seed_demo_drivers`), 2 open
needs (`seed_demo_requests`, `DEMO_REQUESTS`), approved + pending
memories (`DEMO_MEMORIES`), and a Deepak×Karma chat
(`seed_demo_conversation`, `is_demo=1`). Hence Matches/Requests/
Chats look used before the presenter acts.

Fix: `--clean` seed mode = users + twins + profiles (+ memories)
only; no needs, offers, matches, notifications, conversations.
Default seed unchanged (tests pin it).

## 6. Post/offer flow feels like email + directory

Posting an offer is a structured form
(`app/templates/opportunity_form.html`: kind/title/description/
skills/years/location/remote/category); starting contact picks
counterpart twins. No natural-language posting path exists.

Fix: `/post` accepts one NL description, infers need-vs-offer +
slots, shows a concise review, publishes through the existing
engines, and triggers real matching.

## 7. What works and is reused as-is

- Auth/sessions/CSRF/rate limits (`app/auth.py`, routes).
- Requests engine lifecycle (`app/requests.py`).
- Event-driven matcher, fulfillment, dedup, expiry
  (`app/matcher.py`, scenarios A–F in `tests/test_matcher_flows.py`).
- Twin-to-twin conversations engine (`app/conversations.py`).
- Notifications (`/notifications`, `app/templates/notifications.html`).
- Privacy allowlists (`app/privacy.py`, `twin_public_view`).
- Intent pipeline shape (`app/intent.py` + `app/request_parse.py`)
  after the `am`/skills fixes; mock provider default, $0.
- Import/approval flow, usage dashboard, budget guards.

## 8. Navigation target

`app/templates/base.html` today: Twins(/dashboard), Discover,
Requests, Chats, Notifications, Voice + user chip. No test asserts
these labels (verified by grep), so relabeling is safe.

Target (exact labels): Post Something, Find Something,
My Conversation, Notification, Twins + user chip/logout right.
`/discover`, `/requests/*`, `/voice`, `/me` routes keep working
delinked (regression safety).

## 9. Verification limits (stated upfront)

Python has no browser driver installed (no playwright/selenium;
`pip list` verified), system Chrome exists but cannot be driven
from here, and this sandbox forbids loopback sockets (uvicorn boot
fails with WinError 10013 — established in the prior mission), so
no out-of-process browser test can run in this environment. Plan:
honest launch probe, full-stack in-process verification of every
new flow (routes + templates + forms + sessions + DB), plus a
precise manual browser checklist. Final status must reflect the
missing browser evidence.
