"""Phase 6: ten temporary QA accounts, realistic journeys, safe cleanup.

Runs against the isolated temp QA database (never the demo database):
registers ten ordinary users through real HTTP registration, exercises
varied journeys (search-before-post, post-before-search, quantities,
pause/cancel, decisions, import, concurrency, unauthorized access),
then deletes exactly the manifest-tracked users and verifies no
orphans remain while pre-existing users survive.
"""

import re
import threading
import unittest
import uuid

from app.db import get_connection

from tests.test_e2e_flows import E2ECase, _client, _hidden

QA_PASSWORD = "qa-temp-pass-99"


class TestQATenAccounts(E2ECase):
    def _register_qa(self, client, name, email):
        from app import auth as auth_mod

        # Distinct devices have distinct IPs; the shared test IP would
        # trip the 10-registrations/hour anti-spam limit.
        auth_mod.reset_rate_limits()
        page = client.get("/register").text
        resp = client.post("/register", data={
            "name": name, "email": email, "password": QA_PASSWORD,
            "role": "professional",
            "csrf_token": _hidden(page, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        finally:
            conn.close()
        return row["id"]

    def _twin(self, client, display, skills="", public=True):
        page = client.get("/twin/new").text
        resp = client.post("/twin/new", data={
            "display_name": display, "persona": "helpful",
            "tone": "friendly",
            "csrf_token": _hidden(page, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        if not public and not skills:
            return
        page = client.get("/twin/edit").text
        data = {"display_name": display, "persona": "helpful",
                "tone": "friendly", "skills": skills, "location": "",
                "authorized": ["skills"],
                "csrf_token": _hidden(page, "csrf_token")}
        if public:
            data["public_listing"] = "on"
        resp = client.post("/twin/edit", data=data, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)

    def _search(self, client, text, duration="none", quantity=None):
        from app.request_parse import parse_need

        page = client.get("/requests/new").text
        resp = client.post("/requests/new", data={
            "need_text": text, "category": "other",
            "idem_key": uuid.uuid4().hex,
            "csrf_token": _hidden(page, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        req_id = resp.headers["location"].split("/")[2]
        review = client.get(f"/requests/{req_id}/review").text
        parsed = parse_need(text)["requirements"]
        resp = client.post(f"/requests/{req_id}/confirm", data={
            "category": "other",
            "budget": str(parsed.get("budget_max", "")),
            "origin": parsed.get("origin", ""),
            "destination": parsed.get("destination", ""),
            "when": parsed.get("when_text", ""),
            "skills": ", ".join(parsed.get("skills", [])),
            "duration": duration,
            "quantity": "" if quantity is None else str(quantity),
            "csrf_token": _hidden(review, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        return req_id

    def _offer(self, client, title, description="", skills=""):
        page = client.get("/opportunities/new").text
        resp = client.post("/opportunities/new", data={
            "kind": "service", "title": title, "description": description,
            "skills": skills, "years": "", "location": "Thimphu",
            "remote_ok": "", "category": "",
            "csrf_token": _hidden(page, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)

    def _match_ids(self, page):
        return sorted(set(re.findall(r"/matches/(\d+)/accept", page)))

    def _act(self, client, method_path, match_id, decision):
        page = client.get(method_path).text
        return client.post(f"/matches/{match_id}/{decision}", data={
            "csrf_token": _hidden(page, "csrf_token")},
            follow_redirects=False)

    def test_ten_accounts_journeys_and_cleanup(self):
        run = uuid.uuid4().hex[:8]
        clients = []
        for _ in range(12):
            clients.append(_client())
            self.addCleanup(clients[-1].close)
        # Pre-existing users first (NOT in the manifest): cleanup must
        # preserve them.
        keep_ids = [
            self._register_qa(clients[10], "KeepA",
                              f"keep-a-{run}@example.org"),
            self._register_qa(clients[11], "KeepB",
                              f"keep-b-{run}@example.org"),
        ]
        self._twin(clients[10], "KeepATwin")
        # Ten manifest-tracked QA users through real registration.
        manifest = []
        names = ["Asha", "Bikash", "Chimi", "Dorji", "Eden",
                 "Femi", "Gagan", "Hana", "Iris", "Jigme"]
        for i, name in enumerate(names):
            uid = self._register_qa(
                clients[i], name, f"qa-{run}-{i}@example.org")
            manifest.append(uid)
        self.assertEqual(len(set(manifest)), 10)

        # Varied twins: mixed skills, one stays private.
        skills = ["Python", "driving", "design", "tutoring", "Java",
                  "cooking", "web", "plumbing", "music", ""]
        for i in range(10):
            self._twin(clients[i], f"{names[i]}Twin",
                       skills=skills[i], public=(i != 9))

        # Journey 1: search-before-post (job), both accept.
        r0 = self._search(
            clients[0], "Hi twin, find me a fullStack Developer job.",
            duration="1w")
        self._offer(clients[1], "Full-Stack Developer",
                    "Full-stack developer role building web apps.",
                    skills="Python")
        page0 = clients[0].get(f"/requests/{r0}").text
        self.assertIn("Full-Stack Developer", page0)
        mid = self._match_ids(page0)[0]
        resp = self._act(clients[0], f"/requests/{r0}", mid, "accept")
        self.assertEqual(resp.status_code, 303)
        incoming = clients[1].get("/requests").text
        resp = self._act(clients[1], "/requests",
                         self._match_ids(incoming)[0], "accept")
        self.assertEqual(resp.status_code, 303)

        # Journey 2: pause/resume then post-before-search (taxi).
        r2 = self._search(
            clients[2], "Find me a taxi going from Thimphu to Dagana.",
            duration="5h")
        page = clients[2].get(f"/requests/{r2}").text
        resp = clients[2].post(f"/requests/{r2}/pause", data={
            "csrf_token": _hidden(page, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("paused", clients[2].get(f"/requests/{r2}").text)
        page = clients[2].get(f"/requests/{r2}").text
        resp = clients[2].post(f"/requests/{r2}/resume", data={
            "csrf_token": _hidden(page, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self._offer(clients[3], "Thimphu Dagana taxi",
                    "Taxi from Thimphu to Dagana every morning.")
        page2 = clients[2].get(f"/requests/{r2}").text
        self.assertIn("Thimphu Dagana taxi", page2)

        # Journey 3: quantity search, partial fulfillment by two riders.
        r4 = self._search(
            clients[4], "Find me four passengers going from Thimphu"
            " to Dagana.")
        self._search(clients[5],
                     "Need a ride from Thimphu to Dagana tomorrow.")
        self._search(clients[6],
                     "Need a ride from Thimphu to Dagana on Friday.")
        page4 = clients[4].get(f"/requests/{r4}").text
        # Two rider needs plus user 2's earlier taxi search: all three
        # are legitimate demand-side counterparts on the same route.
        self.assertEqual(len(self._match_ids(page4)), 3)
        for match_id in self._match_ids(page4)[:2]:
            resp = self._act(clients[4], f"/requests/{r4}", match_id,
                             "accept")
            self.assertEqual(resp.status_code, 303)
        page4 = clients[4].get(f"/requests/{r4}").text
        self.assertIn("Accepted 2 of 4", page4)

        # Journey 4: rental match, peer declines.
        r7 = self._search(
            clients[7], "Find someone renting out a car for the weekend.")
        self._offer(clients[8], "Weekend car rental",
                    "My car is available for rent on weekends.")
        page7 = clients[7].get(f"/requests/{r7}").text
        self.assertIn("Weekend car rental", page7)
        incoming8 = clients[8].get("/requests").text
        resp = self._act(clients[8], "/requests",
                         self._match_ids(incoming8)[0], "reject")
        self.assertEqual(resp.status_code, 303)

        # Journey 5: private twins never auto-match; publishing
        # plus a fresh search connects the same custom post.
        self._offer(clients[9], "Piano lessons on weekends",
                    "Patient piano teacher for beginners.")
        r5b = self._search(
            clients[5], "I want to learn piano from a teacher.")
        self.assertNotIn("Matches for this search",
                         clients[5].get(f"/requests/{r5b}").text)
        page9 = clients[9].get("/twin/edit").text
        resp = clients[9].post("/twin/edit", data={
            "display_name": "JigmeTwin", "persona": "helpful",
            "tone": "friendly", "skills": "", "location": "",
            "public_listing": "on",
            "csrf_token": _hidden(page9, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        r5c = self._search(
            clients[5], "Seeking weekend piano lessons for beginners.")
        self.assertIn("Piano lessons on weekends",
                      clients[5].get(f"/requests/{r5c}").text)

        # Journey 6: knowledge import + approve by user 1.
        fixture = (__import__("pathlib").Path(__file__).resolve().parent
                   / "fixtures" / "sample_export.json")
        mem_page = clients[1].get("/memories").text
        resp = clients[1].post("/memories/import", data={
            "csrf_token": _hidden(mem_page, "csrf_token")}, files={
            "export_file": ("conversations.json", fixture.read_bytes(),
                            "application/json")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("imported-2", resp.headers["location"])

        # Journey 7: unauthorized access refused everywhere.
        self.assertEqual(
            clients[9].get(f"/requests/{r0}").status_code, 404)
        foreign = self._match_ids(
            clients[2].get(f"/requests/{r2}").text)
        self.assertTrue(foreign)
        noises = clients[9].get("/requests/new").text
        resp = clients[9].post(f"/matches/{foreign[0]}/accept", data={
            "csrf_token": _hidden(noises, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 404)

        # Journey 8: interleaved concurrent reads + posts, no dupes.
        errors: list = []

        def hammer(client, idx):
            import asyncio

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                for path in ("/requests", "/discover",
                             "/notifications", "/me"):
                    resp = client.get(path)
                    assert resp.status_code == 200, (idx, path)
            except Exception as exc:  # noqa: BLE001 - collected
                errors.append(exc)
            finally:
                try:
                    loop.close()
                finally:
                    asyncio.set_event_loop(None)

        threads = [threading.Thread(target=hammer, args=(clients[i], i))
                   for i in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(errors, [])
        conn = get_connection()
        try:
            dupes = conn.execute(
                "SELECT search_request_id, counterpart_type, counterpart_id,"
                " COUNT(*) AS n FROM matches GROUP BY 1, 2, 3"
                " HAVING n > 1").fetchall()
            self.assertEqual(list(dupes), [])
        finally:
            conn.close()

        # Journey 9: logout blocks, re-login restores.
        out_page = clients[0].get("/me").text
        resp = clients[0].post("/logout", data={
            "csrf_token": _hidden(out_page, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        resp = clients[0].get("/requests", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        login_page = clients[0].get("/login").text
        resp = clients[0].post("/login", data={
            "email": f"qa-{run}-0@example.org", "password": QA_PASSWORD,
            "csrf_token": _hidden(login_page, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(clients[0].get("/requests").status_code, 200)

        # ---- Cleanup: exactly the manifest, nothing else. ----
        conn = get_connection()
        try:
            placeholders = ", ".join("?" for _ in manifest)
            conn.execute(
                f"DELETE FROM users WHERE id IN ({placeholders})",
                manifest)
            conn.commit()
            remaining = conn.execute(
                f"SELECT COUNT(*) AS n FROM users WHERE id IN"
                f" ({placeholders})", manifest).fetchone()["n"]
            self.assertEqual(remaining, 0)
            # No orphaned QA records anywhere.
            orphans = conn.execute(
                "SELECT (SELECT COUNT(*) FROM twins WHERE user_id IN"
                f" ({placeholders}))"
                " + (SELECT COUNT(*) FROM requests WHERE owner_user_id IN"
                f" ({placeholders}))"
                " + (SELECT COUNT(*) FROM opportunities"
                f" WHERE owner_user_id IN ({placeholders}))"
                " + (SELECT COUNT(*) FROM notifications WHERE user_id IN"
                f" ({placeholders})) AS n",
                manifest * 4).fetchone()["n"]
            self.assertEqual(orphans, 0)
            dangling_matches = conn.execute(
                "SELECT COUNT(*) AS n FROM matches m LEFT JOIN requests r"
                " ON r.id = m.search_request_id WHERE r.id IS NULL"
                ).fetchone()["n"]
            self.assertEqual(dangling_matches, 0)
            dangling_convos = conn.execute(
                "SELECT COUNT(*) AS n FROM conversations c LEFT JOIN twins"
                " ta ON ta.id = c.twin_a_id LEFT JOIN twins tb"
                " ON tb.id = c.twin_b_id"
                " WHERE ta.id IS NULL OR tb.id IS NULL").fetchone()["n"]
            self.assertEqual(dangling_convos, 0)
            # Pre-existing users and their data survive intact.
            kept = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE id IN (?, ?)",
                keep_ids).fetchone()["n"]
            self.assertEqual(kept, 2)
        finally:
            conn.close()
        # App still healthy for the survivors.
        self.assertEqual(clients[10].get("/requests").status_code, 200)
        self.assertEqual(clients[10].get("/discover").status_code, 200)


if __name__ == "__main__":
    unittest.main()
