"""Mandatory demo scenarios A-F through the real HTTP stack."""

import re
import unittest
import uuid
from pathlib import Path

from app import matcher

from tests.test_e2e_flows import E2ECase, _client, _hidden


def _match_ids(page):
    return sorted(set(re.findall(r"/matches/(\d+)/accept", page)))


class ScenarioCase(E2ECase):
    def _login_client(self, name, email):
        client = _client()
        self.addCleanup(client.close)
        return self._register(client, name, email)

    def _publish(self, client, display, skills="", location=""):
        page = client.get("/twin/edit").text
        resp = client.post("/twin/edit", data={
            "display_name": display, "persona": "helpful",
            "tone": "friendly", "skills": skills, "location": location,
            "authorized": ["skills", "location"],
            "public_listing": "on",
            "csrf_token": _hidden(page, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)

    def _post_offer(self, client, title, kind="job", skills="",
                    category="", description="", location="Thimphu"):
        page = client.get("/opportunities/new").text
        resp = client.post("/opportunities/new", data={
            "kind": kind, "title": title, "description": description,
            "skills": skills, "years": "", "location": location,
            "remote_ok": "", "category": category,
            "csrf_token": _hidden(page, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)

    def _post_search(self, client, text, category="other",
                     duration="none", quantity=None):
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
        self.assertIn("How long should your twin search", review)
        # Submit the pre-filled form unchanged, like a real user
        # accepting the parsed proposal.
        parsed = parse_need(text)["requirements"]
        resp = client.post(f"/requests/{req_id}/confirm", data={
            "category": category,
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


class TestScenarioA_Fullstack(ScenarioCase):
    def test_developer_search_matches_posted_offer(self):
        deepak = self._login_client("Deepak", "deepak.a@example.org")
        karma = self._login_client("Karma", "karma.a@example.org")
        self._make_twin(deepak, "Dee")
        self._make_twin(karma, "Kay")
        self._publish(deepak, "Dee", skills="Python, JavaScript")
        self._publish(karma, "Kay")
        req_id = self._post_search(
            deepak, "Hi twin, find me a fullStack Developer job.",
            duration="1w")
        detail = deepak.get(f"/requests/{req_id}").text
        self.assertIn("search until", detail)
        self.assertIn("Accepted 0 of 1", detail)
        # Nothing matches yet: the search stays active and waits.
        self.assertNotIn("Matches for this search", detail)
        self._post_offer(karma, "Full-Stack Developer", kind="job",
                         skills="Python, JavaScript",
                         description="Full-stack developer role building"
                         " web apps with Python and JavaScript.")
        # Saving the post triggered matching inline: both sides see it.
        detail = deepak.get(f"/requests/{req_id}").text
        self.assertIn("Matches for this search", detail)
        self.assertIn("Full-Stack Developer", detail)
        incoming = karma.get("/requests").text
        self.assertIn("Interest in my posts", incoming)
        self.assertIn("Full-Stack Developer", incoming)
        # Both notified.
        self.assertIn("New match", deepak.get("/notifications").text)
        self.assertIn("has interest", karma.get("/notifications").text)
        # Deepak accepts: coordination chat starts, bounded.
        match_id = _match_ids(detail)[0]
        resp = deepak.post(f"/matches/{match_id}/accept", data={
            "csrf_token": _hidden(detail, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        detail = deepak.get(f"/requests/{req_id}").text
        self.assertIn("Accepted 1 of 1", detail)
        self.assertIn("fulfilled", detail)
        self.assertIn("Chats for this request", detail)
        # Karma accepts on her side too; nothing implies commitment.
        incoming = karma.get("/requests").text
        karma_match = _match_ids(incoming)[0]
        resp = karma.post(f"/matches/{karma_match}/accept", data={
            "csrf_token": _hidden(incoming, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        bodies = "\n".join([
            deepak.get(f"/requests/{req_id}").text,
            karma.get("/requests").text,
            deepak.get("/notifications").text,
        ]).lower()
        for banned in ("suitability", "candidate score",
                       "hiring verdict", "recommended for hire",
                       "interview assessment"):
            self.assertNotIn(banned, bodies)


class TestScenarioB_Taxi(ScenarioCase):
    def test_taxi_search_waits_then_matches(self):
        tashi = self._login_client("Tashi", "tashi.b@example.org")
        driver = self._login_client("Driver", "driver.b@example.org")
        self._make_twin(tashi, "Tash")
        self._make_twin(driver, "Dri")
        self._publish(tashi, "Tash")
        self._publish(driver, "Dri")
        req_id = self._post_search(
            tashi, "Find me a taxi going from Thimphu to Dagana.",
            category="transport", duration="5h")
        detail = tashi.get(f"/requests/{req_id}").text
        self.assertIn("search until", detail)
        self.assertNotIn("Matches for this search", detail)
        self.assertIn("origin", detail)
        self._post_offer(driver, "Thimphu Dagana taxi", kind="service",
                         category="transport", location="Thimphu",
                         description="Taxi from Thimphu to Dagana"
                         " every morning.")
        detail = tashi.get(f"/requests/{req_id}").text
        self.assertIn("Matches for this search", detail)
        self.assertIn("same route", detail)
        incoming = driver.get("/requests").text
        self.assertIn("Thimphu Dagana taxi", incoming)
        match_id = _match_ids(detail)[0]
        resp = tashi.post(f"/matches/{match_id}/accept", data={
            "csrf_token": _hidden(detail, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("Accepted 1 of 1",
                      tashi.get(f"/requests/{req_id}").text)


class TestScenarioC_Passengers(ScenarioCase):
    def _rider(self, name, email):
        client = self._login_client(name, email)
        self._make_twin(client, name[:2])
        self._publish(client, name[:2])
        self._post_search(
            client, "Need a ride from Thimphu to Dagana tomorrow.")
        return client

    def test_four_passengers_partial_fulfillment(self):
        pema = self._login_client("Pema", "pema.c@example.org")
        self._make_twin(pema, "Pem")
        self._publish(pema, "Pem")
        req_id = self._post_search(
            pema, "Find me four passengers going from Thimphu to"
            " Dagana, deadline two hours.")
        detail = pema.get(f"/requests/{req_id}").text
        # Parsed quantity and deadline persisted without asking again.
        self.assertIn("Accepted 0 of 4", detail)
        self.assertIn("search until", detail)
        riders = [self._rider(f"Rider{i}", f"rider{i}.c@example.org")
                  for i in range(4)]
        detail = pema.get(f"/requests/{req_id}").text
        self.assertEqual(len(_match_ids(detail)), 4)
        first = _match_ids(detail)[0]
        resp = pema.post(f"/matches/{first}/accept", data={
            "csrf_token": _hidden(detail, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        detail = pema.get(f"/requests/{req_id}").text
        self.assertIn("Accepted 1 of 4", detail)
        self.assertIn("3 remaining", detail)
        self.assertNotIn("fulfilled", detail)
        for match_id in _match_ids(detail):
            resp = pema.post(f"/matches/{match_id}/accept", data={
                "csrf_token": _hidden(
                    pema.get(f"/requests/{req_id}").text, "csrf_token")},
                follow_redirects=False)
            self.assertEqual(resp.status_code, 303)
        detail = pema.get(f"/requests/{req_id}").text
        self.assertIn("Accepted 4 of 4", detail)
        self.assertIn("fulfilled", detail)
        # A rejection reopens the fulfilled search.
        resp = riders[0].post(f"/matches/{first}/reject", data={
            "csrf_token": _hidden(riders[0].get("/requests").text,
                                  "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        detail = pema.get(f"/requests/{req_id}").text
        self.assertIn("Accepted 3 of 4", detail)
        self.assertNotIn("fulfilled", detail)


class TestScenarioD_CarRental(ScenarioCase):
    def test_rental_intent_matches(self):
        renter = self._login_client("Renter", "renter.d@example.org")
        owner = self._login_client("Owner", "owner.d@example.org")
        self._make_twin(renter, "Ren")
        self._make_twin(owner, "Own")
        self._publish(renter, "Ren")
        self._publish(owner, "Own")
        req_id = self._post_search(
            renter, "Find someone renting out a car for the weekend.",
            duration="1d")
        self._post_offer(owner, "Weekend car rental", kind="service",
                         description="My car is available for rent"
                         " on weekends.")
        detail = renter.get(f"/requests/{req_id}").text
        self.assertIn("Weekend car rental", detail)
        self.assertIn("Interest in my posts", owner.get("/requests").text)


class TestScenarioE_Custom(ScenarioCase):
    def test_non_hardcoded_discovery(self):
        seeker = self._login_client("Seeker", "seeker.e@example.org")
        teacher = self._login_client("Teacher", "teacher.e@example.org")
        self._make_twin(seeker, "See")
        self._make_twin(teacher, "Tea")
        self._publish(seeker, "See")
        self._publish(teacher, "Tea")
        self._post_offer(teacher, "Piano lessons on weekends",
                         kind="service",
                         description="Patient piano teacher for"
                         " beginners.")
        req_id = self._post_search(
            seeker, "I want to learn piano on weekends from a teacher.")
        detail = seeker.get(f"/requests/{req_id}").text
        self.assertIn("Piano lessons on weekends", detail)
        incoming = teacher.get("/requests").text
        self.assertIn("Piano lessons on weekends", incoming)
        self.assertIn("learn piano", incoming)


class TestScenarioF_Expiry(ScenarioCase):
    def test_expire_notify_extend_restart(self):
        from app.db import get_connection

        user = self._login_client("Urgen", "urgen.f@example.org")
        self._make_twin(user, "Urg")
        self._publish(user, "Urg")
        req_id = self._post_search(user, "Need a plumber urgently.",
                                   duration="1h")
        conn = get_connection()
        try:
            # Controlled clock: expire without waiting an hour.
            future = matcher.plus_minutes(matcher.now_stamp(), 61)
            counts = matcher.process_due(conn, future)
            self.assertEqual(counts["expired"], 1)
        finally:
            conn.close()
        detail = user.get(f"/requests/{req_id}").text
        self.assertIn("expired", detail)
        self.assertIn("Extend &amp; resume", detail)
        notes = user.get("/notifications").text
        self.assertIn("search period ended", notes)
        # Extend only with owner authorization, via the real route.
        resp = user.post(f"/requests/{req_id}/extend", data={
            "duration": "1d",
            "csrf_token": _hidden(detail, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        detail = user.get(f"/requests/{req_id}").text
        self.assertIn("search until", detail)
        self.assertNotIn("expired", detail.split("search until")[0])
        # Restart persistence: fresh connections, same file.
        from app.db import init_db

        init_db()
        again = user.get(f"/requests/{req_id}").text
        self.assertIn("search until", again)
        self.assertIn("Need a plumber urgently", again)


class TestUpdateTriggers(ScenarioCase):
    def test_meaningful_edit_matches_noop_does_not_duplicate(self):
        seeker = self._login_client("Seeka", "seeka.g@example.org")
        helper = self._login_client("Helpb", "helpb.g@example.org")
        self._make_twin(seeker, "Sk")
        self._make_twin(helper, "Hp")
        self._publish(seeker, "Sk")
        self._publish(helper, "Hp")
        req_id = self._post_search(
            seeker, "Need help fixing my roof this week.")
        self._post_offer(helper, "Odd jobs", kind="service",
                         description="General help.")
        self.assertNotIn("Matches for this search",
                         seeker.get(f"/requests/{req_id}").text)
        # A meaningful edit (roof repair) triggers matching.
        from app.db import get_connection
        from app.db import get_opportunity_by_owner_title

        conn = get_connection()
        try:
            from app.db import get_user_by_email, get_twin_by_user

            me = get_user_by_email(conn, "helpb.g@example.org")
            opp = get_opportunity_by_owner_title(conn, me["id"],
                                                 "Odd jobs")
            opp_id = opp["id"]
        finally:
            conn.close()
        page = helper.get(f"/opportunities/{opp_id}").text
        resp = helper.post(f"/opportunities/{opp_id}/edit", data={
            "title": "Roof repair service",
            "description": "We fix roofs and leaks every week.",
            "skills": "", "years": "", "location": "Thimphu",
            "remote_ok": "", "category": "",
            "csrf_token": _hidden(page, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("Roof repair service",
                      seeker.get(f"/requests/{req_id}").text)
        before = len(_match_ids(seeker.get(f"/requests/{req_id}").text))
        # A no-op save re-triggers the event but creates no duplicate.
        page = helper.get(f"/opportunities/{opp_id}").text
        resp = helper.post(f"/opportunities/{opp_id}/edit", data={
            "title": "Roof repair service",
            "description": "We fix roofs and leaks every week.",
            "skills": "", "years": "", "location": "Thimphu",
            "remote_ok": "", "category": "",
            "csrf_token": _hidden(page, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        after = len(_match_ids(seeker.get(f"/requests/{req_id}").text))
        self.assertEqual(before, after)
        self.assertEqual(before, 1)

    def test_cross_user_match_decision_refused(self):
        first = self._login_client("First", "first.g@example.org")
        second = self._login_client("Second", "second.g@example.org")
        stranger = self._login_client("Stranger", "str.g@example.org")
        self._make_twin(first, "Fi")
        self._make_twin(second, "Se")
        self._make_twin(stranger, "St")
        self._publish(first, "Fi")
        self._publish(second, "Se")
        req_id = self._post_search(
            first, "Need a logo designer for my startup.")
        self._post_offer(second, "Logo design studio", kind="service",
                         description="We design logos for startups.")
        detail = first.get(f"/requests/{req_id}").text
        match_id = _match_ids(detail)[0]
        token = _hidden(stranger.get("/requests/new").text, "csrf_token")
        resp = stranger.post(f"/matches/{match_id}/accept", data={
            "csrf_token": token},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
