"""Regression tests for the Requests list page (GET /requests).

Covers the 500 caused by the route calling a request-listing helper on
the wrong module: the handler must return 200, show the empty state
with no requests, list existing requests, redirect anonymous users to
login, and never leak one user's requests to another.

The route handler is called directly with an in-process Request (no
TestClient sockets), so this module also runs on sandboxed runners.
Uses an isolated temp SQLite database; never production data.
"""

import os
import tempfile
import unittest
from pathlib import Path

from starlette.requests import Request

from app import auth
from app import config as config_mod
from app import db as db_mod


def _request(path="/requests", user_id=None):
    headers = [(b"host", b"testserver")]
    if user_id is not None:
        token = auth.create_session(user_id)
        headers.append(
            (b"cookie", f"{auth.SESSION_COOKIE}={token}".encode("latin-1")))
    scope = {
        "type": "http", "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": path, "raw_path": path.encode("ascii"),
        "query_string": b"", "headers": headers,
        "server": ("testserver", 80), "client": ("testclient", 50000),
    }
    return Request(scope)


class RequestsPageCase(unittest.TestCase):
    def setUp(self):
        from app.routes import requests as requests_route

        self.route = requests_route
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "reqpage.db")
        self._saved_env = {
            key: os.environ.get(key)
            for key in ("DATABASE_PATH", "MIRA_TEST_DATABASE_URL")}
        os.environ["DATABASE_PATH"] = self.db
        # Force the temp SQLite file even when PG test config is present.
        os.environ["MIRA_TEST_DATABASE_URL"] = "sqlite"
        self._saved_settings = db_mod.settings
        db_mod.settings = config_mod.get_settings()
        db_mod.init_db()
        conn = db_mod.get_connection()
        try:
            self.ada = db_mod.create_user(
                conn, name="Ada", role="professional",
                email="ada.reqpage@example.org", password_hash=None)
            self.ada_twin = db_mod.create_twin(
                conn, user_id=self.ada, display_name="AdaTwin",
                persona="helpful", tone="warm", profile={},
                authorized_fields=[], avatar_seed="ada-reqpage")
            self.bob = db_mod.create_user(
                conn, name="Bob", role="professional",
                email="bob.reqpage@example.org", password_hash=None)
            self.bob_twin = db_mod.create_twin(
                conn, user_id=self.bob, display_name="BobTwin",
                persona="helpful", tone="warm", profile={},
                authorized_fields=[], avatar_seed="bob-reqpage")
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        db_mod.settings = self._saved_settings
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def _make_request(self, twin_id, owner_id, need_text, key):
        conn = db_mod.get_connection()
        try:
            req_id = db_mod.create_request(
                conn, twin_id=twin_id, owner_user_id=owner_id,
                category="transport", need_text=need_text,
                idempotency_key=key)
            conn.commit()
        finally:
            conn.close()
        return req_id

    def test_empty_state_returns_200(self):
        resp = self.route.request_list(_request(user_id=self.ada))
        self.assertEqual(resp.status_code, 200)
        body = resp.body.decode("utf-8")
        self.assertIn("No requests yet", body)
        self.assertIn("New request", body)

    def test_existing_requests_are_listed(self):
        need = "Unit-test taxi ride from GCIT to Thimphu"
        req_id = self._make_request(self.ada_twin, self.ada, need, "rp-1")
        resp = self.route.request_list(_request(user_id=self.ada))
        self.assertEqual(resp.status_code, 200)
        body = resp.body.decode("utf-8")
        self.assertIn(need, body)
        self.assertIn("transport", body)
        self.assertIn("AdaTwin", body)
        self.assertIn(f"/requests/{req_id}", body)

    def test_anonymous_user_redirects_to_login(self):
        resp = self.route.request_list(_request())
        self.assertEqual(resp.status_code, 303)
        self.assertTrue(resp.headers["location"].endswith("/login"))

    def test_requests_are_not_shared_between_users(self):
        need = "Ada private errand across town"
        self._make_request(self.ada_twin, self.ada, need, "rp-2")
        resp = self.route.request_list(_request(user_id=self.bob))
        self.assertEqual(resp.status_code, 200)
        body = resp.body.decode("utf-8")
        self.assertNotIn(need, body)
        self.assertIn("No requests yet", body)


if __name__ == "__main__":
    unittest.main()
