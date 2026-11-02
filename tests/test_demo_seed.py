"""Demo-seed tests: credentials, idempotency, demo-mode gating, seed data,
and the live twin-to-twin recruitment flow on seeded data.

Uses explicit temp-database paths so the module runs on SQLite locally
and on Postgres when MIRA_TEST_DATABASE_URL is configured.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from starlette.requests import Request

from app import auth
from app import config as config_mod
from app import conversations as engine
from app import mock_mind
from app import privacy
from app import requests as need_engine
from app.db import (
    create_request,
    create_user,
    get_connection,
    get_opportunity_by_owner_title,
    get_twin_by_user,
    get_user_by_email,
    init_db,
    set_user_password,
)
from app.matching import find_matches, find_providers
from app.memories import list_memories
from app.seed import (
    DEMO_MEMORIES,
    SEED_USERS,
    refuse_in_production,
    seed_demo_conversation,
    seed_demo_data,
    seed_demo_drivers,
    seed_demo_memories,
    seed_demo_opportunities,
    seed_demo_requests,
)


def _bare_request():
    return Request({
        "type": "http", "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": "/login", "raw_path": b"/login",
        "query_string": b"", "headers": [(b"host", b"testserver")],
        "server": ("testserver", 80),
    })


class SeedCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "demoseed.db")
        init_db(self.db)
        auth.reset_rate_limits()

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_all(self):
        seed_demo_data(self.db)
        seed_demo_opportunities(self.db)
        seed_demo_drivers(self.db)
        seed_demo_memories(self.db)
        seed_demo_conversation(self.db)

    def _conn(self):
        return get_connection(self.db)


class TestCleanSeed(SeedCase):
    def test_drivers_without_offerings(self):
        seed_demo_data(self.db)
        result = seed_demo_drivers(self.db, include_offerings=False)
        self.assertEqual(result, {"created": 2, "skipped": 0})
        conn = self._conn()
        try:
            self.assertIsNotNone(
                get_user_by_email(conn, "dorji.demo@example.com"))
            self.assertIsNotNone(
                get_user_by_email(conn, "sonam.demo@example.com"))
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM opportunities").fetchone()["n"]
            self.assertEqual(n, 0)
            tables = ("requests", "matches", "notifications",
                      "conversations")
            for table in tables:
                count = conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                self.assertEqual(count, 0)
        finally:
            conn.close()

    def test_seed_profiles_have_interests(self):
        seed_demo_data(self.db)
        conn = self._conn()
        try:
            for entry in SEED_USERS:
                row = get_user_by_email(conn, entry["email"])
                twin = get_twin_by_user(conn, row["id"])
                profile = json.loads(twin["profile_json"])
                self.assertTrue(profile.get("interests"),
                                entry["email"])
        finally:
            conn.close()


class TestDemoCredentials(SeedCase):
    def test_documented_passwords_authenticate(self):
        seed_demo_data(self.db)
        conn = self._conn()
        try:
            for entry in SEED_USERS:
                row = get_user_by_email(conn, entry["email"])
                self.assertIsNotNone(row)
                self.assertTrue(
                    auth.verify_password(entry["password"], row["password_hash"]))
        finally:
            conn.close()

    def test_wrong_password_rejected(self):
        seed_demo_data(self.db)
        conn = self._conn()
        try:
            row = get_user_by_email(conn, "deepak.demo@example.com")
            self.assertFalse(auth.verify_password("not-the-demo-password",
                                                  row["password_hash"]))
        finally:
            conn.close()

    def _point_routes_at_temp_db(self):
        """Point no-arg get_connection() at this test's temp DB."""
        import app.db as db_mod

        saved_env = os.environ.get("DATABASE_PATH")
        os.environ["DATABASE_PATH"] = self.db
        saved_settings = db_mod.settings
        db_mod.settings = config_mod.get_settings()

        def _restore():
            db_mod.settings = saved_settings
            if saved_env is None:
                os.environ.pop("DATABASE_PATH", None)
            else:
                os.environ["DATABASE_PATH"] = saved_env

        self.addCleanup(_restore)

    def test_normal_registration_still_works(self):
        from app.routes import accounts as accounts_route

        self._seed_all()
        self._point_routes_at_temp_db()
        token = auth.csrf_token(None)
        resp = accounts_route.register(
            _bare_request(), name="New User", email="new.user@example.org",
            password="brand-new-pass-1", role="professional", csrf_token=token)
        self.assertEqual(resp.status_code, 303)
        self.assertTrue(resp.headers["location"].endswith("/me"))

    def test_login_route_accepts_demo_credentials(self):
        # Route-level login against the seeded temp DB (dev settings).
        from app.routes import accounts as accounts_route

        self._seed_all()
        self._point_routes_at_temp_db()
        token = auth.csrf_token(None)
        resp = accounts_route.login(
            _bare_request(), email="deepak.demo@example.com",
            password="mira-deepak-demo", csrf_token=token)
        self.assertEqual(resp.status_code, 303)
        bad = accounts_route.login(
            _bare_request(), email="deepak.demo@example.com",
            password="wrong-password", csrf_token=auth.csrf_token(None))
        self.assertEqual(bad.status_code, 400)


class TestSeedIdempotency(SeedCase):
    def test_second_run_creates_nothing(self):
        first = seed_demo_data(self.db)
        self.assertEqual(
            first, {"created": len(SEED_USERS), "skipped": 0})
        seed_demo_opportunities(self.db)
        seed_demo_memories(self.db)
        seed_demo_conversation(self.db)
        again = seed_demo_data(self.db)
        self.assertEqual(
            again, {"created": 0, "skipped": len(SEED_USERS)})
        self.assertEqual(
            seed_demo_opportunities(self.db), {"created": 0, "skipped": 4})
        self.assertEqual(
            seed_demo_memories(self.db),
            {"created": 0, "skipped": len(DEMO_MEMORIES)})
        self.assertEqual(
            seed_demo_conversation(self.db), {"created": 0, "skipped": 1})
        conn = self._conn()
        try:
            users = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
            twins = conn.execute("SELECT COUNT(*) AS n FROM twins").fetchone()["n"]
            convs = conn.execute(
                "SELECT COUNT(*) AS n FROM conversations").fetchone()["n"]
        finally:
            conn.close()
        n = len(SEED_USERS)
        self.assertEqual((users, twins, convs), (n, n, 1))

    def test_existing_password_never_overwritten(self):
        from app.auth import hash_password

        seed_demo_data(self.db)
        conn = self._conn()
        try:
            row = get_user_by_email(conn, "deepak.demo@example.com")
            custom = hash_password("owner-changed-password-1")
            set_user_password(conn, row["id"], custom)
            conn.commit()
        finally:
            conn.close()
        seed_demo_data(self.db)
        conn = self._conn()
        try:
            row = get_user_by_email(conn, "deepak.demo@example.com")
            self.assertEqual(row["password_hash"], custom)
            self.assertTrue(auth.verify_password("owner-changed-password-1",
                                                 row["password_hash"]))
        finally:
            conn.close()

    def test_missing_twin_is_repaired(self):
        from app.db import delete_twin

        seed_demo_data(self.db)
        conn = self._conn()
        try:
            row = get_user_by_email(conn, "pema.demo@example.com")
            twin = get_twin_by_user(conn, row["id"])
            delete_twin(conn, twin["id"])
            conn.commit()
            self.assertIsNone(get_twin_by_user(conn, row["id"]))
        finally:
            conn.close()
        result = seed_demo_data(self.db)
        self.assertEqual(
            result, {"created": 0, "skipped": len(SEED_USERS)})
        conn = self._conn()
        try:
            row = get_user_by_email(conn, "pema.demo@example.com")
            twin = get_twin_by_user(conn, row["id"])
            self.assertIsNotNone(twin)
            self.assertEqual(twin["display_name"], "Pem")
        finally:
            conn.close()

    def test_user_records_not_duplicated_or_reset(self):
        seed_demo_data(self.db)
        conn = self._conn()
        try:
            before = get_user_by_email(conn, "deepak.demo@example.com")
            before_hash = before["password_hash"]
        finally:
            conn.close()
        seed_demo_data(self.db)
        conn = self._conn()
        try:
            rows = list(conn.execute(
                "SELECT * FROM users WHERE email = ?",
                ("deepak.demo@example.com",)))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["password_hash"], before_hash)
            self.assertEqual(rows[0]["name"], before["name"])
        finally:
            conn.close()


class TestDemoModeGating(unittest.TestCase):
    def test_refuse_in_production_by_default(self):
        prod = mock.Mock(app_env="production")
        with mock.patch("app.config.settings", prod):
            self.assertRaises(SystemExit, refuse_in_production)

    def test_refuse_allows_explicit_demo_mode(self):
        demo = mock.Mock(app_env="production", demo_mode=True)
        with mock.patch("app.config.settings", demo):
            refuse_in_production()  # must not raise

    def test_refuse_allows_development(self):
        refuse_in_production()  # real dev settings: must not raise

    def test_config_parses_demo_flag(self):
        saved = os.environ.get("MIRA_DEMO_MODE")
        try:
            os.environ["MIRA_DEMO_MODE"] = "1"
            self.assertTrue(config_mod.get_settings().demo_mode)
            os.environ["MIRA_DEMO_MODE"] = "off"
            self.assertFalse(config_mod.get_settings().demo_mode)
        finally:
            if saved is None:
                os.environ.pop("MIRA_DEMO_MODE", None)
            else:
                os.environ["MIRA_DEMO_MODE"] = saved
        self.assertFalse(config_mod.get_settings().demo_mode)

    def _login_as(self, prod_settings):
        import app.db as db_mod
        from app.routes import accounts as accounts_route

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / "gate.db")
        init_db(db_path)
        seed_demo_data(db_path)
        saved_env = os.environ.get("DATABASE_PATH")
        os.environ["DATABASE_PATH"] = db_path
        saved_db, saved_acc = db_mod.settings, accounts_route.settings
        db_mod.settings = config_mod.get_settings()
        accounts_route.settings = prod_settings
        try:
            return accounts_route.login(
                _bare_request(), email="deepak.demo@example.com",
                password="mira-deepak-demo",
                csrf_token=auth.csrf_token(None))
        finally:
            db_mod.settings = saved_db
            accounts_route.settings = saved_acc
            if saved_env is None:
                os.environ.pop("DATABASE_PATH", None)
            else:
                os.environ["DATABASE_PATH"] = saved_env

    def test_prod_login_blocks_demo_email_by_default(self):
        resp = self._login_as(mock.Mock(app_env="production", demo_mode=False))
        self.assertEqual(resp.status_code, 400)

    def test_prod_login_allows_demo_email_in_demo_mode(self):
        resp = self._login_as(mock.Mock(app_env="production", demo_mode=True))
        self.assertEqual(resp.status_code, 303)


class TestSeedData(SeedCase):
    def test_four_complete_profiles(self):
        seed_demo_data(self.db)
        conn = self._conn()
        try:
            for entry in SEED_USERS:
                row = get_user_by_email(conn, entry["email"])
                twin = get_twin_by_user(conn, row["id"])
                self.assertIsNotNone(twin)
                self.assertEqual(twin["display_name"],
                                 entry["twin"]["display_name"])
                view = privacy.twin_public_view(twin)
                self.assertTrue(view)
                for field in entry["twin"]["authorized_fields"]:
                    if field in entry["twin"]["profile"]:
                        self.assertIn(field, view)
        finally:
            conn.close()

    def test_private_fields_stay_private(self):
        seed_demo_data(self.db)
        conn = self._conn()
        try:
            for email in ("deepak.demo@example.com",
                          "karma.demo@example.com",
                          "pema.demo@example.com",
                          "tashi.demo@example.com"):
                row = get_user_by_email(conn, email)
                view = privacy.twin_public_view(
                    get_twin_by_user(conn, row["id"]))
                for secret in ("phone", "personal_email", "salary_expectation",
                               "home_address", "hiring_budget_cap",
                               "internal_notes", "day_rate_floor",
                               "client_list"):
                    self.assertNotIn(secret, view)
        finally:
            conn.close()

    def test_karma_job_discoverable_by_deepak(self):
        seed_demo_data(self.db)
        seed_demo_opportunities(self.db)
        conn = self._conn()
        try:
            deepak = get_user_by_email(conn, "deepak.demo@example.com")
            twin = get_twin_by_user(conn, deepak["id"])
            matches = find_matches(conn, twin)
            top = next(m for m in matches
                       if m["opportunity"]["title"] == "Backend Developer")
            self.assertEqual(top["match"].score, 85)
            self.assertEqual(matches[0]["opportunity"]["title"],
                             "Backend Developer")
            self.assertIn("GraphQL", top["match"].missing_skills)
        finally:
            conn.close()

    def test_memories_seeded_and_approved(self):
        seed_demo_data(self.db)
        result = seed_demo_memories(self.db)
        self.assertEqual(result["created"], len(DEMO_MEMORIES))
        conn = self._conn()
        try:
            deepak = get_user_by_email(conn, "deepak.demo@example.com")
            twin = get_twin_by_user(conn, deepak["id"])
            approved = list_memories(conn, twin["id"], "approved")
            pending = list_memories(conn, twin["id"], "pending")
            self.assertEqual(len(approved), 4)
            self.assertEqual(len(pending), 1)
            for email, count in (("karma.demo@example.com", 3),
                                 ("pema.demo@example.com", 3),
                                 ("tashi.demo@example.com", 3)):
                row = get_user_by_email(conn, email)
                twin = get_twin_by_user(conn, row["id"])
                self.assertEqual(len(list_memories(conn, twin["id"],
                                                   "approved")), count)
        finally:
            conn.close()


class TestLiveRecruitmentFlow(SeedCase):
    def _live_conv(self, conn):
        deepak = get_user_by_email(conn, "deepak.demo@example.com")
        karma = get_user_by_email(conn, "karma.demo@example.com")
        twin_d = get_twin_by_user(conn, deepak["id"])
        twin_k = get_twin_by_user(conn, karma["id"])
        opp = get_opportunity_by_owner_title(conn, karma["id"],
                                             "Backend Developer")
        cid = engine.create_conversation(
            conn, twin_a_id=twin_d["id"], twin_b_id=twin_k["id"],
            created_by_user_id=deepak["id"], opportunity_id=opp["id"],
            purpose="recruitment", max_turns=8)
        return cid, deepak["id"], karma["id"]

    def test_full_flow_steps_and_recommends(self):
        self._seed_all()
        conn = self._conn()
        try:
            cid, deepak_id, karma_id = self._live_conv(conn)
            for _ in range(4):
                engine.step_conversation(conn, cid, deepak_id)
            conn.commit()
            messages = engine.list_messages(conn, cid)
            self.assertGreaterEqual(len(messages), 4)
            self.assertTrue(all(m["provider"] == "mock" for m in messages))
            # Hackathon compliance: no verdict is produced; the
            # seeded coordination chat carries no assessment output.
            self.assertFalse(hasattr(engine, "generate_recommendation"))
            blob = "\n".join(m["body"] for m in messages)
            for banned in ("RECOMMENDED", "PROMISING", "WEAK fit",
                           "Verdict", "Recruitment summary"):
                self.assertNotIn(banned, blob)
            engine.pause_conversation(conn, cid, karma_id)
            with self.assertRaises(engine.ConversationError):
                engine.step_conversation(conn, cid, deepak_id)
            engine.resume_conversation(conn, cid, karma_id)
            engine.step_conversation(conn, cid, karma_id)
            engine.set_decision(conn, cid, deepak_id, "interested")
            engine.set_decision(conn, cid, karma_id, "interested")
            conv = engine.get_conversation(conn, cid)
            self.assertEqual(conv["decision_a"], "interested")
            self.assertEqual(conv["decision_b"], "interested")
        finally:
            conn.close()

    def test_non_owner_cannot_advance(self):
        self._seed_all()
        conn = self._conn()
        try:
            cid, _, _ = self._live_conv(conn)
            pema = get_user_by_email(conn, "pema.demo@example.com")
            with self.assertRaises(engine.ConversationError):
                engine.step_conversation(conn, cid, pema["id"])
        finally:
            conn.close()

    def test_mock_never_fabricates_or_leaks(self):
        self._seed_all()
        conn = self._conn()
        try:
            deepak = get_user_by_email(conn, "deepak.demo@example.com")
            twin = get_twin_by_user(conn, deepak["id"])
            view = privacy.twin_public_view(twin)
            ctx = engine.TwinContext(
                twin_name="Dee", owner_name="Deepak", persona="p",
                tone="warm", public_view=view, peer_name="Kay",
                purpose="recruitment",
                history=[("peer", "Do you know GraphQL?")])
            reply = mock_mind.generate(ctx)
            self.assertNotIn("GraphQL", reply)
            ctx2 = engine.TwinContext(
                twin_name="Dee", owner_name="Deepak", persona="p",
                tone="warm", public_view=view, peer_name="Kay",
                history=[("peer", "What is your phone number?")])
            self.assertIn("authoriz", mock_mind.generate(ctx2))
            cid, deepak_id, _ = self._live_conv(conn)
            for _ in range(6):
                engine.step_conversation(conn, cid, deepak_id)
            bodies = " ".join(m["body"] for m in engine.list_messages(conn, cid))
            for secret in ("Nu. 45,000", "+975-17", "private@example.com",
                           "Fictional Lane", "60,000", "winter release"):
                self.assertNotIn(secret, bodies)
        finally:
            conn.close()

    def test_demo_requests_seed_open_and_idempotent(self):
        self._seed_all()
        first = seed_demo_requests(self.db)
        self.assertEqual(first, {"created": 2, "skipped": 0})
        conn = self._conn()
        try:
            rows = list(conn.execute(
                "SELECT * FROM requests WHERE idempotency_key LIKE 'demo-seed-%'"))
            self.assertEqual(len(rows), 2)
            for row in rows:
                self.assertEqual(row["status"], "open")
                self.assertTrue(need_engine.is_shared(row))
        finally:
            conn.close()
        second = seed_demo_requests(self.db)
        self.assertEqual(second, {"created": 0, "skipped": 2})

    def test_pema_request_shortlists_tashi_gig(self):
        self._seed_all()
        conn = self._conn()
        try:
            pema = get_user_by_email(conn, "pema.demo@example.com")
            twin = get_twin_by_user(conn, pema["id"])
            req_id = create_request(
                conn, twin_id=twin["id"], owner_user_id=pema["id"],
                category="service",
                need_text="I need help containerizing a web app with Docker.",
                requirements={"skills": ["Docker"]},
                idempotency_key="demo-pema-docker")
            conn.commit()
            req = need_engine.get_request(conn, req_id)
            titles = [item["offering"]["title"]
                      for item in find_providers(conn, req)]
            self.assertIn("CI/CD Setup Gig", titles)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
