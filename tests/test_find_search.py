"""Structured Find Something searches: Jobs / Taxi / Others.

Each search type shows only its own fields, queries real stored posts
through the existing deterministic rules (no invented results, no AI,
no twin writes), and degrades to a clear empty state. The twin chat
below the search panel keeps working unchanged.
"""

import unittest
import uuid

from tests.test_e2e_flows import E2ECase, _client, _hidden


class FindSearchCase(E2ECase):
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

    def _twin_skills(self, email):
        from app.db import (get_connection, get_twin_by_user,
                            get_user_by_email, twin_profile)
        conn = get_connection(self.db)
        try:
            user = get_user_by_email(conn, email)
            twin = get_twin_by_user(conn, user["id"])
            return list(twin_profile(twin).get("skills", []) or [])
        finally:
            conn.close()


class TestJobSearch(FindSearchCase):
    def test_title_search_returns_stored_job(self):
        employer = self._login_client("Employer", "boss.fj@example.org")
        seeker = self._login_client("Seeker", "seeker.fj@example.org")
        self._make_twin(employer, "BossT")
        self._make_twin(seeker, "SeekT")
        self._publish(employer, "BossT")
        self._publish(seeker, "SeekT", skills="wiring, circuits")
        opp_id = self._post_offer(
            employer, "Electrician", skills="wiring, circuits",
            description="Licensed electrician for home rewiring work.",
            category="job")
        body = seeker.get("/find?type=job&title=Electrician").text
        self.assertIn("Job results (1)", body)
        self.assertIn("Electrician", body)
        self.assertIn(f"/opportunities/{opp_id}", body)
        self.assertIn("BossT", body)
        self.assertIn("Match ", body)
        # Case-insensitive partial words match too.
        body = seeker.get("/find?type=job&title=ELECTR").text
        self.assertIn("Job results (1)", body)
        # Words from the description match weakly.
        body = seeker.get("/find?type=job&title=rewiring").text
        self.assertIn("Job results (1)", body)

    def test_title_search_empty_state_and_exclusions(self):
        from app.db import get_connection

        employer = self._login_client("Employer", "boss.fe@example.org")
        seeker = self._login_client("Seeker", "seeker.fe@example.org")
        self._make_twin(employer, "BossT")
        self._make_twin(seeker, "SeekT")
        self._publish(employer, "BossT")
        self._publish(seeker, "SeekT")
        opp_id = self._post_offer(
            employer, "Driver", skills="driving",
            description="Delivery driver needed.", category="job")
        # A non-job post with similar words is not a job result.
        self._post_offer(
            employer, "Driving lessons", skills="driving",
            description="Learn driving from an expert.", kind="service",
            category="service", location="Thimphu")
        body = seeker.get("/find?type=job&title=driver").text
        self.assertIn("Job results (1)", body)
        self.assertIn(f"/opportunities/{opp_id}", body)
        self.assertNotIn("Driving lessons", body)
        # Nothing stored, nothing invented.
        body = seeker.get("/find?type=job&title=astronaut").text
        self.assertIn("No jobs match that title", body)
        # Optional location filter narrows; remote wording still works.
        body = seeker.get("/find?type=job&title=driver&location=Paro").text
        self.assertIn("No jobs match that title", body)
        body = seeker.get(
            "/find?type=job&title=driver&location=Thimphu").text
        self.assertIn("Job results (1)", body)
        # Own posts and expired offers never appear.
        body = employer.get("/find?type=job&title=driver").text
        self.assertIn("No jobs match that title", body)
        conn = get_connection(self.db)
        try:
            conn.execute("UPDATE opportunities SET expires_at = ?"
                         " WHERE id = ?",
                         ("2000-01-01 00:00:00", int(opp_id)))
            conn.commit()
        finally:
            conn.close()
        body = seeker.get("/find?type=job&title=driver").text
        self.assertIn("No jobs match that title", body)

    def test_employer_requirements_stay_off_twin_skills(self):
        employer = self._login_client("Employer", "boss.fs@example.org")
        seeker = self._login_client("Seeker", "seeker.fs@example.org")
        self._make_twin(employer, "BossT")
        self._make_twin(seeker, "SeekT")
        self._publish(employer, "BossT", skills="management")
        self._publish(seeker, "SeekT", skills="Python")
        self._post_offer(
            employer, "Accountant", skills="bookkeeping, taxation",
            description="Accountant for a Thimphu firm.", category="job")
        seeker.get("/find?type=job&title=accountant")
        self.assertEqual(self._twin_skills("boss.fs@example.org"),
                         ["management"])
        self.assertEqual(self._twin_skills("seeker.fs@example.org"),
                         ["Python"])


class TestTaxiSearch(FindSearchCase):
    def _seed_taxi(self):
        driver = self._login_client("Driver", "driver.ft@example.org")
        rider = self._login_client("Rider", "rider.ft@example.org")
        self._make_twin(driver, "DriveT")
        self._make_twin(rider, "RideT")
        self._publish(driver, "DriveT")
        self._publish(rider, "RideT")
        opp_id = self._post_offer(
            driver, "Taxi service", kind="service", category="transport",
            description="Taxi available from Thimphu to Paro every morning.",
            location="Thimphu")
        req_id = self._post_search(
            rider, "I need a taxi from Thimphu to Paro tomorrow.",
            category="transport")
        return driver, rider, opp_id, req_id

    def test_route_search_returns_rides(self):
        driver, rider, opp_id, _ = self._seed_taxi()
        body = rider.get(
            "/find?type=taxi&origin=Thimphu&destination=Paro").text
        self.assertIn("Ride results (1)", body)
        self.assertIn("Taxi service", body)
        self.assertIn(f"/opportunities/{opp_id}", body)
        self.assertIn("Same route", body)
        # The driver finds the rider's need, with a contact action.
        body = driver.get(
            "/find?type=taxi&origin=Thimphu&destination=Paro").text
        self.assertIn("Ride results (1)", body)
        self.assertIn("taxi from Thimphu to Paro", body)
        self.assertIn("from Thimphu", body)
        self.assertIn("to Paro", body)
        self.assertIn("/discover/needs/", body)
        self.assertIn("Have DriveT answer RideT", body)

    def test_route_normalization_and_empty_state(self):
        _, rider, _, _ = self._seed_taxi()
        # Case and whitespace do not matter; one endpoint suffices.
        body = rider.get(
            "/find?type=taxi&origin=++thimphu++&destination=+PARO+").text
        self.assertIn("Ride results (1)", body)
        body = rider.get("/find?type=taxi&origin=Thimphu").text
        self.assertIn("Ride results (1)", body)
        # Unrelated routes and job titles stay out of taxi results.
        body = rider.get(
            "/find?type=taxi&origin=Gelephu&destination=Sarpang").text
        self.assertIn("No rides cover that route", body)
        body = rider.get("/find?type=taxi&title=Electrician").text
        self.assertIn("going from", body)

    def test_taxi_search_leaves_twins_alone(self):
        driver, rider, _, _ = self._seed_taxi()
        rider.get("/find?type=taxi&origin=Thimphu&destination=Paro")
        driver.get("/find?type=taxi&origin=Thimphu&destination=Paro")
        self.assertEqual(self._twin_skills("driver.ft@example.org"), [])
        self.assertEqual(self._twin_skills("rider.ft@example.org"), [])


class TestOtherSearch(FindSearchCase):
    def test_description_search_returns_posts(self):
        provider = self._login_client("Provider", "prov.fo@example.org")
        seeker = self._login_client("Seeker", "seeker.fo@example.org")
        helper = self._login_client("Helper", "helper.fo@example.org")
        self._make_twin(provider, "ProvT")
        self._make_twin(seeker, "SeekT")
        self._make_twin(helper, "HelpT")
        self._publish(provider, "ProvT")
        self._publish(seeker, "SeekT")
        self._publish(helper, "HelpT")
        opp_id = self._post_offer(
            provider, "Math tutoring", kind="service", category="service",
            skills="mathematics",
            description="Evening math tutoring for high school students.")
        self._post_search(helper, "I offer graphic design services.",
                           category="service")
        body = seeker.get("/find?type=other&q=tutoring+mathematics").text
        self.assertIn("Math tutoring", body)
        self.assertIn(f"/opportunities/{opp_id}", body)
        body = seeker.get("/find?type=other&q=graphic+design").text
        self.assertIn("graphic design", body)
        self.assertIn("/discover/needs/", body)
        # Empty state for genuinely absent posts.
        body = seeker.get("/find?type=other&q=beekeeping+regulation").text
        self.assertIn("Nothing matches that description", body)


class TestSearchShell(FindSearchCase):
    def test_tabs_validation_and_chat_preserved(self):
        from app.db import get_connection, get_user_by_email

        user = self._login_client("User", "user.fx@example.org")
        self._make_twin(user, "MyT")
        page = user.get("/find").text
        for tab in ("stab-job", "stab-taxi", "stab-other"):
            self.assertIn(tab, page)
        self.assertIn(">Jobs<", page)
        self.assertIn(">Taxi<", page)
        self.assertIn(">Others<", page)
        self.assertIn("Job title", page)
        self.assertIn("Going from", page)
        self.assertIn("Going to", page)
        self.assertIn("What are you looking for?", page)
        # Validation messages, not crashes or silent no-ops.
        body = user.get("/find?type=job").text
        self.assertIn("Enter a job title", body)
        body = user.get("/find?type=taxi").text
        self.assertIn("going from", body)
        body = user.get("/find?type=other").text
        self.assertIn("Describe what you are looking for", body)
        body = user.get("/find?type=bogus").text
        self.assertNotIn("results (", body)
        # Results render above the preserved twin chat.
        employer = self._login_client("Employer", "boss.fx@example.org")
        self._make_twin(employer, "BossT")
        self._publish(employer, "BossT")
        self._post_offer(employer, "Plumber", skills="plumbing",
                         description="Plumber for home repairs.",
                         category="job")
        body = user.get("/find?type=job&title=plumber").text
        self.assertLess(body.index("Plumber"), body.index("chat-thread"))
        self.assertIn('name="message"', body)
        self.assertIn("No messages yet", body)
        # The chat still creates real searches.
        resp = user.post("/find", data={
            "message": "I need a plumber tomorrow.",
            "csrf_token": _hidden(body, "csrf_token")},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        conn = get_connection(self.db)
        try:
            owner = get_user_by_email(conn, "user.fx@example.org")
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM requests"
                " WHERE owner_user_id = ?",
                (owner["id"],)).fetchone()["n"]
            self.assertEqual(total, 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
