# MIRA Manual Browser Checklist

Use when automated browser verification is unavailable. Every step was
designed against the real routes and templates; expected results cite
visible strings and database state you can re-query.

## Setup (PowerShell, project root)

```powershell
$env:AI_PROVIDER='mock'
Remove-Item data/demo.db* -ErrorAction SilentlyContinue
.\.python\python.exe -m app.seed --db data/demo.db --clean
$env:AI_PROVIDER='mock'; $env:DATABASE_PATH='data/demo.db'
.\.python\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. Use two browser profiles (A = Deepak,
B = Karma). Demo passwords follow `mira-<name>-demo`.

## 1. Clean start

- [ ] Root shows the public landing ("Your personal AI twin", Create
      account, Log in). Nobody is logged in; no demo data visible.
- [ ] Log in as Deepak (`deepak.demo@example.com`). Root now goes to
      Find Something. Nav reads exactly: Post Something,
      Find Something, My Conversation, Notification, Twins.

## 2. Profile and twin

- [ ] Twins page shows Deepak's twin "Dee" with an Edit twin action.
- [ ] Edit twin shows populated skills (JavaScript, Node.js, Python,
      …), location Thimphu, interests — all editable, with sharing
      checkboxes. Unchecked fields stay private.

## 3. Find Something (Deepak, profile A)

- [ ] Find Something shows "What are you looking for?" and "No
      messages yet".
- [ ] Type `Hi` → Send. Twin greets; no search is created.
- [ ] Type `I'm looking for a full-stack developer job in Bhutan.
      Preferably something relevant to my skills.` → Send (and once
      with Enter).
- [ ] The twin replies "Got it — searching for …", cites "Using your
      profile (skills: …)", and honestly reports "Nothing suitable
      yet" with a keep-looking date. No When/skills/category form.
- [ ] Refresh: history persists; no duplicate search (one request in
      the database for Deepak).

## 4. Post Something (Karma, profile B)

- [ ] Log in as Karma in profile B. Notification and My Conversation
      are empty ("No notifications", "None yet").
- [ ] Post Something → type `I'm hiring a full-stack developer for
      my team. I need someone with JavaScript, Node.js, frontend,
      and backend skills. The role is based in Bhutan.` → Review.
- [ ] Review shows Type Offer, a title, Location Bhutan, parsed
      skills. No recipient/to/from fields. Publish.
- [ ] Karma lands on the new posting. No manual search needed.

## 5. Match, both sides

- [ ] Deepak: Notification shows "New match"; Find Something shows
      the posting card with a relevance reason; opening the search
      shows "Accepted 0 of 1".
- [ ] Karma: Notification shows the interest ("… has interest");
      her Requests page lists it under "Interest in my posts" with
      an "I'm interested" action and her own pending decision.
- [ ] No private data leaks: Deepak sees Karma's company/requirements
      (authorized); Karma sees Deepak's authorized fields only.

## 6. Coordination and decisions

- [ ] Deepak opens the search → Accept. A coordination chat starts;
      My Conversation lists it with both twin names.
- [ ] Karma opens My Conversation → the same chat; her side still
      shows her own pending decision (no implied consent).
- [ ] Karma accepts from the request view; both sides show accepted.
- [ ] In the chat: Auto-play 6 turns exchanges authorized facts;
      Record decision → Interested on each side independently.
- [ ] Refresh both profiles: state persists. Double-submit Accept:
      no duplicate matches or notifications.

## 7. Non-job domain (piano)

- [ ] Dema (profile A, log out/in or third profile): Find Something
      → `I want to find a piano teacher who can teach beginners on
      weekends.` → honest empty or genuine results only.
- [ ] Wangchuk (profile B): Post Something →
      `I can provide piano lessons on weekends for beginners.` →
      Review (Type Offer) → Publish.
- [ ] Dema's chat shows the piano offer despite different wording.

## 8. Taxi quantity (optional)

- [ ] Tashi: Find Something →
      `I need a taxi from Thimphu to Dagana tomorrow. I need four
      seats.` → no route question asked; search shows
      "Accepted 0 of 4".
- [ ] Dorji: Post Something →
      `I have a taxi available from Thimphu to Dagana tomorrow.` →
      Review (Type Offer) → Publish.
- [ ] Tashi sees the match; accepting one shows "Accepted 1 of 4"
      (never "fulfilled"); a rejection restores the remaining count.

## 9. Clean-state proof

- [ ] Before recording, Find/Notification/My Conversation are empty
      for a fresh login, and the database has 0 rows in requests,
      opportunities, matches, notifications, conversations.
- [ ] Usage page shows $0 on the mock provider after all flows.

## Recording failures

Note the page, action, visible error, and server log lines; reseed
fresh (`Remove-Item data/demo.db*` + seed `--clean`) and retry from
the last verified step. Never edit the database to fake a result.
