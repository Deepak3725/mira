"""One-account demo-flow tests: everything the 3:15 video needs must be
reachable from a single logged-in demo account (Deepak), plus the slim
public landing page (no marketing roadmap, no demo data, no auto-login).
"""

import os
import tempfile
import unittest
from pathlib import Path

from starlette.requests import Request

from app import auth
from app import config as config_mod
from app import db as db_mod
from app.db import (
    get_connection,
    get_opportunity_by_owner_title,
    get_twin_by_user,
    get_user_by_email,
    init_db,
)
from app.seed import (
    seed_demo_conversation,
    seed_demo_data,
    seed_demo_drivers,
    seed_demo_memories,
    seed_demo_opportunities,
)


def _request(path="/", cookie=None):
    headers = [(b"host", b"testserver")]
    if cookie:
        headers.append((b"cookie", cookie.encode("latin-1")))
    return Request({
        "type": "http", "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": path, "raw_path": path.encode("ascii"),
        "query_string": b"", "headers": headers,
        "server": ("testserver", 80), "client": ("testclient", 50000),
    })


class DemoFlowCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "demoflow.db")
        init_db(self.db)
        seed_demo_data(self.db)
        seed_demo_opportunities(self.db)
        seed_demo_drivers(self.db)
        seed_demo_memories(self.db)
        seed_demo_conversation(self.db)
        auth.reset_rate_limits()
        self._saved_env = os.environ.get("DATABASE_PATH")
        os.environ["DATABASE_PATH"] = self.db
        self._saved_settings = db_mod.settings
        db_mod.settings = config_mod.get_settings()

    def tearDown(self):
        db_mod.settings = self._saved_settings
        if self._saved_env is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = self._saved_env
        self.tmp.cleanup()

    def _deepak_cookie(self):
        conn = get_connection(self.db)
        try:
            uid = get_user_by_email(conn, "deepak.demo@example.com")["id"]
        finally:
            conn.close()
        return f"{auth.SESSION_COOKIE}={auth.create_session(uid)}"


class TestLandingPage(DemoFlowCase):
    # Product spec: the public landing is slim and functional — no
    # marketing roadmap, no demo data, no auto-login. Logged-in
    # visitors go straight into the app.
    def test_anonymous_landing_is_public_and_clean(self):
        from app.routes import pages as pages_route

        resp = pages_route.index(_request("/"))
        self.assertEqual(resp.status_code, 200)
        body = resp.body.decode("utf-8")
        self.assertIn("Your personal AI twin", body)
        self.assertIn('href="/register"', body)
        self.assertIn('href="/login"', body)
        for leaked in ("deepak.demo@example.com", "Deepak",
                       "Prototype today", "Planned next"):
            self.assertNotIn(leaked, body)

    def test_logged_in_root_redirects_to_find(self):
        from app.routes import pages as pages_route

        resp = pages_route.index(
            _request("/", self._deepak_cookie()))
        self.assertEqual(resp.status_code, 303)
        self.assertTrue(
            resp.headers["location"].endswith("/find"))


class TestSingleAccountReach(DemoFlowCase):
    def test_deepak_discovers_karma_job(self):
        from app.routes import pages as pages_route

        conn = get_connection(self.db)
        try:
            deepak = get_user_by_email(conn, "deepak.demo@example.com")
            twin = get_twin_by_user(conn, deepak["id"])
        finally:
            conn.close()
        resp = pages_route.discover(
            _request("/discover", self._deepak_cookie()),
            twin_id=twin["id"], skills="", location="")
        self.assertEqual(resp.status_code, 200)
        body = resp.body.decode("utf-8")
        self.assertIn("Backend Developer", body)
        self.assertIn("Match 85/100", body)

    def test_discover_never_scores_another_twin(self):
        # Hackathon compliance: a twin_id naming someone else's twin
        # is ignored -- only the viewer's own twin is scored.
        from app.routes import pages as pages_route

        conn = get_connection(self.db)
        try:
            deepak = get_user_by_email(conn, "deepak.demo@example.com")
            karma = get_user_by_email(conn, "karma.demo@example.com")
            own = get_twin_by_user(conn, deepak["id"])
            other = get_twin_by_user(conn, karma["id"])
        finally:
            conn.close()
        resp = pages_route.discover(
            _request("/discover", self._deepak_cookie()),
            twin_id=other["id"], skills="", location="")
        self.assertEqual(resp.status_code, 200)
        body = resp.body.decode("utf-8")
        self.assertIn(own["display_name"], body)
        self.assertNotIn("Browse as", body)

    def test_anonymous_discover_shows_no_scores(self):
        from app.routes import pages as pages_route

        resp = pages_route.discover(
            _request("/discover"), twin_id=None, skills="",
            location="")
        self.assertEqual(resp.status_code, 200)
        body = resp.body.decode("utf-8")
        self.assertIn("Backend Developer", body)
        self.assertNotIn("Match ", body)

    def test_deepak_views_job_with_owner_distinction(self):
        from app.routes import pages as pages_route

        conn = get_connection(self.db)
        try:
            karma = get_user_by_email(conn, "karma.demo@example.com")
            opp = get_opportunity_by_owner_title(
                conn, karma["id"], "Backend Developer")
        finally:
            conn.close()
        resp = pages_route.opportunity_detail(
            _request(f"/opportunities/{opp['id']}", self._deepak_cookie()),
            opp["id"])
        self.assertEqual(resp.status_code, 200)
        body = resp.body.decode("utf-8")
        # Employer clearly distinguished from the logged-in account.
        self.assertIn("Kay", body)
        self.assertIn("Karma", body)
        self.assertIn("Have Dee contact Kay", body)
        self.assertIn('name="twin_a_id"', body)

    def test_demo_chat_readable_without_login(self):
        from app.routes import chats as chats_route

        conn = get_connection(self.db)
        try:
            cid = conn.execute(
                "SELECT id FROM conversations WHERE is_demo = 1 LIMIT 1"
            ).fetchone()["id"]
        finally:
            conn.close()
        resp = chats_route.convo_detail(_request(f"/conversations/{cid}"), cid)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("read-only", resp.body.decode("utf-8"))

    def test_deepak_dashboard_shows_journey_and_actions(self):
        from app.routes import accounts as accounts_route

        resp = accounts_route.my_space(
            _request("/me", self._deepak_cookie()))
        self.assertEqual(resp.status_code, 200)
        body = resp.body.decode("utf-8")
        for marker in ("My twin journey", "My twin", "My conversations",
                       "/twin/edit", "/memories", "/usage",
                       "/conversations"):
            self.assertIn(marker, body)
        for gone in ("/voice", "Talk", "Test", "Activate", "Control"):
            self.assertNotIn(gone, body)


if __name__ == "__main__":
    unittest.main()
