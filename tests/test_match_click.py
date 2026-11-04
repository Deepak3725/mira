"""Match-notification click flow: regression tests for HTTP 500s.

Covers the complete workflow -- a match is stored, a notification is
created, the recipient opens the notification and clicks through to
the match/request details -- plus graceful handling of missing,
expired, deleted, unauthorized, corrupt, and crafted inputs.

Note on mechanics: the in-process test client re-raises unhandled
route exceptions (Starlette's ServerErrorMiddleware always re-raises
after sending the 500), so an HTTP 500 manifests here as a test
error, never a silent pass.
"""

import re
import unittest
import uuid

from tests.test_e2e_flows import E2ECase, _client, _hidden


def _notif_ids(page):
    return sorted(set(re.findall(r'name="notif_id" value="(\d+)"', page)))


def _match_ids(page):
    return sorted(set(re.findall(r"/matches/(\d+)/accept", page)))


class MatchClickCase(E2ECase):
    def _login_client(self, name, email):
        client = _client()
        self.addCleanup(client.close)
        return self._register(client, name, email)

    def _publish(self, client, display, skills="", location="",
                 phone="", personal_email=""):
        page = client.get("/twin/edit").text
        data = {
            "display_name": display, "persona": "helpful",
            "tone": "friendly", "skills": skills, "location": location,
            "phone": phone, "personal_email": personal_email,
            "authorized": ["skills", "location"],
            "public_listing": "on",
            "csrf_token": _hidden(page, "csrf_token")}
        resp = client.post("/twin/edit", data=data,
                           follow_redirects=False)
        self.assertEqual(resp.status_code, 303)

    def _post_offer(self, client, title, skills="", description="",
                    kind="job", category=""):
        page = client.get("/opportunities/new").text
        resp = client.post("/opportunities/new", data={
            "kind": kind, "title": title, "description": description,
            "skills": skills, "years": "", "location": "Thimphu",
            "remote_ok": "", "category": category,
            "csrf_token": _hidden(page, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        return resp.headers["location"].split("/")[2].split("?")[0]

    def _post_search(self, client, text, category="other"):
        from app.request_parse import parse_need

        page = client.get("/requests/new").text
        resp = client.post("/requests/new", data={
            "need_text": text, "category": category,
            "idem_key": uuid.uuid4().hex,
            "csrf_token": _hidden(page, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        req_id = resp.headers["location"].split("/")[2]
        review = client.get(f"/requests/{req_id}/review").text
        parsed = parse_need(text)["requirements"]
        resp = client.post(f"/requests/{req_id}/confirm", data={
            "category": category,
            "budget": str(parsed.get("budget_max", "")),
            "origin": parsed.get("origin", ""),
            "destination": parsed.get("destination", ""),
            "when": parsed.get("when_text", ""),
            "skills": ", ".join(parsed.get("skills", [])),
            "duration": "none", "quantity": "",
            "csrf_token": _hidden(review, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        return req_id

    def _csrf(self, client):
        return _hidden(client.get("/find").text, "csrf_token")

    def _click_all_notifications(self, client):
        """Follow every notification via mark-read redirect (must be 200)."""
        seen = []
        for nid in _notif_ids(client.get("/notifications").text):
            resp = client.post(
                "/notifications/read",
                data={"notif_id": nid,
                      "csrf_token": self._csrf(client)},
                follow_redirects=False)
            self.assertEqual(resp.status_code, 303)
            target = resp.headers["location"]
            seen.append(target)
            if target.startswith("/"):
                page = client.get(target)
                self.assertEqual(page.status_code, 200)
        return seen

    def _notification_kinds(self, email):
        from app.db import (get_connection, get_user_by_email,
                            list_notifications)
        conn = get_connection(self.db)
        try:
            user = get_user_by_email(conn, email)
            return [n["kind"] for n in
                    list_notifications(conn, user["id"])]
        finally:
            conn.close()


class TestNotificationClickFlow(MatchClickCase):
    def test_click_match_notification_opens_details(self):
        seeker = self._login_client("Seeker", "seeker.mc@example.org")
        provider = self._login_client("Provider", "provider.mc@example.org")
        self._make_twin(seeker, "SeekT")
        self._make_twin(provider, "ProvT")
        self._publish(seeker, "SeekT", skills="Python, JavaScript")
        self._publish(provider, "ProvT")
        req_id = self._post_search(
            seeker, "Hi twin, find me a fullStack Developer job.")
        self._post_offer(
            provider, "Full-Stack Developer", skills="Python, JavaScript",
            description="Full-stack developer role building web apps"
            " with Python and JavaScript.")
        # Exactly one match notification per side (no duplicates).
        self.assertEqual(
            self._notification_kinds("seeker.mc@example.org"), ["match"])
        self.assertEqual(
            self._notification_kinds("provider.mc@example.org"), ["match"])
        self.assertIn("New match", seeker.get("/notifications").text)
        self.assertIn("has interest", provider.get("/notifications").text)
        # Clicking each notification lands on a working details page.
        seen = self._click_all_notifications(seeker)
        self.assertIn(f"/requests/{req_id}", seen)
        detail = seeker.get(f"/requests/{req_id}").text
        self.assertIn("Matches for this search", detail)
        self.assertIn("Full-Stack Developer", detail)
        seen = self._click_all_notifications(provider)
        self.assertIn("/requests", seen)
        incoming = provider.get("/requests").text
        self.assertIn("Interest in my posts", incoming)
        self.assertIn("Full-Stack Developer", incoming)

    def test_accept_flow_and_contact_unlock(self):
        seeker = self._login_client("Seeker", "seeker.ma@example.org")
        provider = self._login_client("Provider", "provider.ma@example.org")
        self._make_twin(seeker, "SeekT")
        self._make_twin(provider, "ProvT")
        self._publish(seeker, "SeekT", skills="Python",
                      phone="+975-17-111111",
                      personal_email="seeker.ma@example.org")
        self._publish(provider, "ProvT", phone="+975-17-222222",
                      personal_email="provider.ma@example.org")
        req_id = self._post_search(
            seeker, "Looking for a Python developer job in Thimphu.")
        self._post_offer(
            provider, "Python Developer", skills="Python",
            description="Python developer needed in Thimphu.")
        detail = seeker.get(f"/requests/{req_id}").text
        # Contact stays hidden until both sides accept.
        self.assertNotIn("+975-17-222222", detail)
        self.assertNotIn("provider.ma@example.org", detail)
        match_id = _match_ids(detail)[0]
        resp = seeker.post(f"/matches/{match_id}/accept", data={
            "message": "Interview Monday?", "csrf_token": self._csrf(seeker)},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        # One-sided acceptance still hides contact; the peer is told
        # where to respond (the coordination chat).
        detail = seeker.get(f"/requests/{req_id}").text
        self.assertNotIn("+975-17-222222", detail)
        self.assertIn("Chats for this request", detail)
        seen = self._click_all_notifications(provider)
        self.assertTrue(any(s.startswith("/conversations/") for s in seen))
        incoming = provider.get("/requests").text
        self.assertNotIn("+975-17-111111", incoming)
        resp = provider.post(f"/matches/{match_id}/accept", data={
            "csrf_token": self._csrf(provider)}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        # Mutual acceptance unlocks contact for the two participants.
        detail = seeker.get(f"/requests/{req_id}").text
        self.assertIn("+975-17-222222", detail)
        self.assertIn("provider.ma@example.org", detail)
        incoming = provider.get("/requests").text
        self.assertIn("+975-17-111111", incoming)
        self.assertIn("seeker.ma@example.org", incoming)
        # Nobody else can see it.
        stranger = self._login_client("Stranger", "stranger.ma@example.org")
        self._make_twin(stranger, "StrT")
        self.assertEqual(
            stranger.get(f"/requests/{req_id}",
                         follow_redirects=False).status_code, 404)
        resp = stranger.post(f"/matches/{match_id}/accept", data={
            "csrf_token": self._csrf(stranger)}, follow_redirects=False)
        self.assertEqual(resp.status_code, 404)

    def test_reject_never_reappears(self):
        from app import matcher
        from app.db import get_connection

        seeker = self._login_client("Seeker", "seeker.mr@example.org")
        provider = self._login_client("Provider", "provider.mr@example.org")
        self._make_twin(seeker, "SeekT")
        self._make_twin(provider, "ProvT")
        self._publish(seeker, "SeekT")
        self._publish(provider, "ProvT")
        req_id = self._post_search(
            seeker, "Looking for piano lessons in Thimphu.")
        self._post_offer(
            provider, "Piano lessons", skills="piano",
            description="Piano lessons for beginners in Thimphu.",
            kind="service", category="service")
        detail = seeker.get(f"/requests/{req_id}").text
        match_id = _match_ids(detail)[0]
        resp = seeker.post(f"/matches/{match_id}/reject", data={
            "csrf_token": self._csrf(seeker)}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        conn = get_connection(self.db)
        try:
            matcher.match_search(conn, int(req_id))
            row = conn.execute("SELECT status FROM matches WHERE id = ?",
                               (int(match_id),)).fetchone()
            self.assertEqual(row["status"], "rejected")
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM matches"
                " WHERE search_request_id = ?",
                (int(req_id),)).fetchone()["n"]
            self.assertEqual(total, 1)
        finally:
            conn.close()
        self.assertEqual(seeker.get(f"/requests/{req_id}").status_code, 200)
        self.assertEqual(provider.get("/requests").status_code, 200)

    def test_missing_expired_unauthorized_graceful(self):
        from app import matcher
        from app.db import get_connection, update_request

        seeker = self._login_client("Seeker", "seeker.mg@example.org")
        stranger = self._login_client("Stranger", "stranger.mg@example.org")
        self._make_twin(seeker, "SeekT")
        self._make_twin(stranger, "StrT")
        req_id = self._post_search(seeker, "Need an electrician in Thimphu.")
        token = self._csrf(seeker)
        # Missing records: 404, never 500.
        self.assertEqual(seeker.get("/requests/9999").status_code, 404)
        self.assertEqual(seeker.get("/opportunities/9999").status_code, 404)
        self.assertEqual(seeker.get("/conversations/9999").status_code, 404)
        resp = seeker.post("/matches/9999/accept",
                           data={"csrf_token": token},
                           follow_redirects=False)
        self.assertEqual(resp.status_code, 404)
        resp = seeker.post("/matches/9999/reject",
                           data={"csrf_token": token},
                           follow_redirects=False)
        self.assertEqual(resp.status_code, 404)
        # Unrelated users: 404, never 500 or a leak.
        self.assertEqual(
            stranger.get(f"/requests/{req_id}").status_code, 404)
        resp = stranger.post("/matches/1/accept", data={
            "csrf_token": self._csrf(stranger)}, follow_redirects=False)
        self.assertEqual(resp.status_code, 404)
        # Expired searches still open; their notification still clicks.
        conn = get_connection(self.db)
        try:
            update_request(conn, int(req_id), status="open",
                           expires_at="2000-01-01 00:00:00")
            conn.commit()
            matcher.process_due(conn, matcher.now_stamp())
        finally:
            conn.close()
        self.assertEqual(seeker.get(f"/requests/{req_id}").status_code, 200)
        seen = self._click_all_notifications(seeker)
        self.assertIn(f"/requests/{req_id}", seen)


class TestClickFlowRobustness(MatchClickCase):
    """Corrupt, null, and crafted inputs must degrade, never 500."""

    def _seed_pair(self):
        seeker = self._login_client("Seeker", "seeker.mcx@example.org")
        provider = self._login_client("Provider", "provider.mcx@example.org")
        self._make_twin(seeker, "SeekT")
        self._make_twin(provider, "ProvT")
        self._publish(seeker, "SeekT", skills="Python")
        self._publish(provider, "ProvT")
        req_id = self._post_search(
            seeker, "Looking for a Python developer job in Thimphu.")
        opp_id = self._post_offer(
            provider, "Python Developer", skills="Python",
            description="Python developer needed in Thimphu.")
        return seeker, provider, req_id, opp_id

    def test_corrupt_opportunity_skills_still_renders(self):
        from app.db import get_connection

        seeker, provider, _, opp_id = self._seed_pair()
        conn = get_connection(self.db)
        try:
            conn.execute("UPDATE opportunities"
                         " SET required_skills_json = 'not-json{{'"
                         " WHERE id = ?", (int(opp_id),))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(
            provider.get(f"/opportunities/{opp_id}").status_code, 200)
        self.assertEqual(seeker.get("/discover").status_code, 200)
        conn = get_connection(self.db)
        try:
            conn.execute("UPDATE opportunities"
                         " SET required_skills_json = '\"justastring\"'"
                         " WHERE id = ?", (int(opp_id),))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(
            provider.get(f"/opportunities/{opp_id}").status_code, 200)

    def test_null_requirement_values_still_render(self):
        from app.db import get_connection

        seeker, _, req_id, _ = self._seed_pair()
        conn = get_connection(self.db)
        try:
            conn.execute("UPDATE requests SET requirements_json = ?"
                         " WHERE id = ?",
                         ('{"origin": null, "skills": null,'
                          ' "budget_max": null}', int(req_id)))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(seeker.get(f"/requests/{req_id}").status_code, 200)
        self.assertEqual(seeker.get("/discover").status_code, 200)

    def test_crafted_notification_ids_never_500(self):
        seeker, _, _, _ = self._seed_pair()
        token = self._csrf(seeker)
        for raw in ("all", " 1 ", "01", "-1", "bogus", "1;DROP",
                    "99999999999999999999999", "9" * 5000):
            resp = seeker.post("/notifications/read",
                               data={"notif_id": raw, "csrf_token": token},
                               follow_redirects=False)
            self.assertEqual(resp.status_code, 303)
        self.assertEqual(seeker.get("/notifications").status_code, 200)

    def test_huge_path_ids_are_404_not_500(self):
        seeker, _, _, _ = self._seed_pair()
        huge = str(2 ** 70)
        self.assertEqual(seeker.get(f"/requests/{huge}").status_code, 404)
        self.assertEqual(
            seeker.get(f"/opportunities/{huge}").status_code, 404)
        self.assertEqual(
            seeker.get(f"/conversations/{huge}").status_code, 404)
        resp = seeker.post(f"/matches/{huge}/accept",
                           data={"csrf_token": self._csrf(seeker)},
                           follow_redirects=False)
        self.assertEqual(resp.status_code, 404)
        resp = seeker.post(f"/matches/{huge}/reject",
                           data={"csrf_token": self._csrf(seeker)},
                           follow_redirects=False)
        self.assertEqual(resp.status_code, 404)


class TestMatchDetails(MatchClickCase):
    """The details view is complete, authorized, and mobile-marked."""

    def _seed_taxi_pair(self):
        driver = self._login_client("Driver", "driver.md@example.org")
        rider = self._login_client("Rider", "rider.md@example.org")
        self._make_twin(driver, "DriveT")
        self._make_twin(rider, "RideT")
        self._publish(driver, "DriveT", phone="+975-17-222222",
                      personal_email="driver.md@example.org")
        self._publish(rider, "RideT", phone="+975-17-111111",
                      personal_email="rider.md@example.org")
        need_id = self._post_search(
            driver, "Taxi available from Thimphu to Paro tomorrow"
            " morning, budget Nu. 800.", category="transport")
        req_id = self._post_search(
            rider, "I need a taxi from Thimphu to Paro tomorrow.",
            category="transport")
        return driver, rider, need_id, req_id

    def _mark_driver_requirements(self, need_id):
        from app.db import get_connection

        conn = get_connection(self.db)
        try:
            conn.execute("UPDATE requests SET requirements_json = ?"
                         " WHERE id = ?",
                         ('{"origin": "Thimphu", "destination": "Paro",'
                          ' "when_text": "Monday dawn",'
                          ' "budget_max": 800}', int(need_id)))
            conn.commit()
        finally:
            conn.close()

    def test_need_counterpart_details_inline(self):
        _, rider, need_id, req_id = self._seed_taxi_pair()
        self._mark_driver_requirements(need_id)
        detail = rider.get(f"/requests/{req_id}").text
        self.assertIn("Matches for this search", detail)
        # Counterpart need text plus shared route facts render inline.
        self.assertIn("Taxi available from Thimphu to Paro", detail)
        self.assertIn("Monday dawn", detail)
        # The structured budget ceiling never crosses to the other
        # side (neither in match details nor in discovery).
        self.assertNotIn("budget_max", detail)
        self.assertNotIn("budget_max", rider.get("/discover").text)

    def test_withdrawn_counterpart_stays_graceful(self):
        driver, rider, need_id, req_id = self._seed_taxi_pair()
        self._mark_driver_requirements(need_id)
        self.assertIn("Monday dawn",
                      rider.get(f"/requests/{req_id}").text)
        resp = driver.post(f"/requests/{need_id}/close", data={
            "csrf_token": self._csrf(driver)}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        detail = rider.get(f"/requests/{req_id}").text
        self.assertEqual(rider.get(f"/requests/{req_id}").status_code, 200)
        # Withdrawn posts stop sharing their facts, without breaking
        # the details view or its remaining actions.
        self.assertNotIn("Monday dawn", detail)
        self.assertIn("Matches for this search", detail)

    def test_contact_visible_only_to_mutual_participants(self):
        from app.db import get_connection

        driver, rider, _, req_id = self._seed_taxi_pair()
        detail = rider.get(f"/requests/{req_id}").text
        match_id = _match_ids(detail)[0]
        resp = rider.post(f"/matches/{match_id}/accept", data={
            "csrf_token": self._csrf(rider)}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        resp = driver.post(f"/matches/{match_id}/accept", data={
            "csrf_token": self._csrf(driver)}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("+975-17-222222",
                      rider.get(f"/requests/{req_id}").text)
        self.assertIn("+975-17-111111", driver.get("/requests").text)
        stranger = self._login_client("Stranger", "stranger.md@example.org")
        self._make_twin(stranger, "StrT")
        # Outsiders see neither the match nor any contact details.
        other = stranger.get("/requests").text
        self.assertNotIn("DriveT", other)
        self.assertNotIn("+975-17-111111", other)
        self.assertNotIn("+975-17-222222", other)
        self.assertNotIn("driver.md@example.org", other)
        self.assertNotIn("rider.md@example.org", other)
        conn = get_connection(self.db)
        try:
            conv = conn.execute(
                "SELECT id FROM conversations ORDER BY id LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(
            stranger.get(f"/conversations/{conv['id']}").status_code, 404)

    def test_key_pages_carry_mobile_viewport(self):
        driver, rider, _, req_id = self._seed_taxi_pair()
        for path in (f"/requests/{req_id}", "/requests", "/find",
                     "/notifications"):
            self.assertIn('name="viewport"',
                          rider.get(path).text)

    def test_unhandled_errors_logged_without_leaking(self):
        import logging

        from app.log import get_logger
        from app.main import _uncaught_errors
        from tests.test_e2e_flows import _run_sandboxed

        class _URL:
            path = "/boom"

        class _Request:
            method = "GET"
            url = _URL()

        records = []

        class _Sink(logging.Handler):
            def emit(self, record):
                records.append(record)

        logger = get_logger()
        sink = _Sink()
        logger.addHandler(sink)
        try:
            try:
                raise ValueError("kablam")
            except ValueError as exc:
                response = _run_sandboxed(
                    _uncaught_errors(_Request(), exc))
        finally:
            logger.removeHandler(sink)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("kablam", response.body.decode("utf-8"))
        self.assertTrue(records)
        self.assertEqual(records[0].error_type, "ValueError")
        self.assertEqual(records[0].path, "/boom")


if __name__ == "__main__":
    unittest.main()
