"""Phase 3A maintained tests: auth, engine, mock brain (stdlib only)."""

import tempfile
import time
import unittest
from pathlib import Path

from app import auth, conversations as engine, mock_mind
from app.conversations import ConversationError, TwinContext
from app.db import (
    create_twin,
    create_user,
    get_twin_by_user,
    get_user_by_email,
    init_db,
)
from app.seed import SEED_USERS, seed_demo_data, seed_demo_opportunities


class EngineCase(unittest.TestCase):
    def setUp(self):
        from app.db import get_connection

        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "t3.db")
        init_db(self.db)
        seed_demo_data(self.db)
        seed_demo_opportunities(self.db)
        self.conn = get_connection(self.db)
        self.deepak = get_user_by_email(self.conn, "deepak.demo@example.com")
        self.karma = get_user_by_email(self.conn, "karma.demo@example.com")
        self.pema = get_user_by_email(self.conn, "pema.demo@example.com")
        self.twin_d = get_twin_by_user(self.conn, self.deepak["id"])
        self.twin_k = get_twin_by_user(self.conn, self.karma["id"])

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _conv(self, **kwargs):
        base = {"twin_a_id": self.twin_d["id"], "twin_b_id": self.twin_k["id"],
                "created_by_user_id": self.deepak["id"]}
        base.update(kwargs)
        return engine.create_conversation(self.conn, **base)


class TestAuth(unittest.TestCase):
    def test_hash_roundtrip(self):
        hashed = auth.hash_password("correct-horse-9")
        self.assertTrue(auth.verify_password("correct-horse-9", hashed))
        self.assertFalse(auth.verify_password("wrong", hashed))
        self.assertFalse(auth.verify_password("x", None))
        self.assertFalse(auth.verify_password("x", "garbage"))

    def test_session_roundtrip_and_tamper(self):
        token = auth.create_session(42)
        self.assertEqual(auth.parse_session(token), 42)
        self.assertIsNone(auth.parse_session(token[:-2] + "zz"))
        self.assertIsNone(auth.parse_session("bogus"))
        expired = auth.create_session(7, ttl=-10)
        self.assertIsNone(auth.parse_session(expired))

    def test_sessions_unique_within_a_second(self):
        # Regression: tokens used to be f(user_id, second), so a
        # logout followed by a same-second re-login re-minted the
        # just-revoked token and the user stayed locked out (flaky
        # 303 in the onboarding journey). Frozen time makes the old
        # collision deterministic; the nonce must prevent it.
        from unittest import mock

        now = time.time()
        with mock.patch.object(auth.time, "time",
                               return_value=now):
            first = auth.create_session(42)
            second = auth.create_session(42)
        self.assertNotEqual(first, second)
        self.assertEqual(auth.parse_session(first), 42)
        self.assertEqual(auth.parse_session(second), 42)

    def test_parse_accepts_legacy_three_part_session(self):
        payload = f"7:{int(time.time()) + 3600}"
        legacy = f"{payload}:{auth._sign(payload)}"
        self.assertEqual(auth.parse_session(legacy), 7)

    def test_rate_limit(self):
        auth.reset_rate_limits()
        key = f"test:{time.monotonic()}"
        for _ in range(3):
            self.assertTrue(auth.check_rate_limit(key, 3, 60))
        self.assertFalse(auth.check_rate_limit(key, 3, 60))

    def test_csrf_token_roundtrip(self):
        token = auth.csrf_token(42)
        self.assertTrue(auth.valid_csrf(token, 42))
        self.assertFalse(auth.valid_csrf(token, 43))
        self.assertFalse(auth.valid_csrf(token, None))
        self.assertFalse(auth.valid_csrf(token[:-2] + "zz", 42))
        self.assertFalse(auth.valid_csrf("", 42))
        self.assertFalse(auth.valid_csrf(None, 42))

    def test_csrf_anonymous_token(self):
        token = auth.csrf_token(None)
        self.assertTrue(auth.valid_csrf(token, None))
        self.assertFalse(auth.valid_csrf(token, 7))

    def test_require_csrf_raises_403(self):
        from fastapi import HTTPException

        auth.require_csrf(auth.csrf_token(9), 9)
        with self.assertRaises(HTTPException) as ctx:
            auth.require_csrf("bogus", 9)
        self.assertEqual(ctx.exception.status_code, 403)


class TestEngineCreate(EngineCase):
    def test_create_and_list(self):
        cid = self._conv()
        conv = engine.get_conversation(self.conn, cid)
        self.assertEqual(conv["status"], "active")
        self.assertEqual(conv["turn"], "a")
        self.assertEqual(conv["name_a"], "Dee")
        mine = engine.list_for_user(self.conn, self.deepak["id"])
        self.assertEqual([c["id"] for c in mine], [cid])
        other = engine.list_for_user(self.conn, self.pema["id"])
        self.assertEqual(other, [])

    def test_create_rejects_bad_input(self):
        with self.assertRaises(ConversationError):
            self._conv(twin_b_id=self.twin_d["id"])
        with self.assertRaises(ConversationError):
            self._conv(twin_b_id=9999)
        with self.assertRaises(ConversationError):
            self._conv(created_by_user_id=self.pema["id"])
        with self.assertRaises(ConversationError):
            self._conv(purpose="telepathy")
        with self.assertRaises(ConversationError):
            self._conv(opportunity_id=9999)


class TestEngineFlow(EngineCase):
    def test_stepping_alternates_and_counts(self):
        cid = self._conv()
        first = engine.step_conversation(self.conn, cid, self.deepak["id"])
        self.assertEqual(first["message"]["sender"], "a")
        self.assertIn("Dee", first["message"]["body"])
        second = engine.step_conversation(self.conn, cid, self.karma["id"])
        self.assertEqual(second["message"]["sender"], "b")
        self.assertEqual(engine.twin_message_count(self.conn, cid), 2)

    def test_outsider_cannot_step(self):
        cid = self._conv()
        with self.assertRaises(ConversationError):
            engine.step_conversation(self.conn, cid, self.pema["id"])
        with self.assertRaises(ConversationError):
            engine.step_conversation(self.conn, cid, None)

    def test_pause_resume_close(self):
        cid = self._conv()
        engine.pause_conversation(self.conn, cid, self.karma["id"])
        with self.assertRaises(ConversationError):
            engine.step_conversation(self.conn, cid, self.deepak["id"])
        engine.resume_conversation(self.conn, cid, self.deepak["id"])
        engine.step_conversation(self.conn, cid, self.deepak["id"])
        engine.close_conversation(self.conn, cid, self.karma["id"])
        with self.assertRaises(ConversationError):
            engine.step_conversation(self.conn, cid, self.deepak["id"])
        with self.assertRaises(ConversationError):
            engine.pause_conversation(self.conn, cid, self.deepak["id"])

    def test_turn_limit_auto_closes(self):
        cid = self._conv(max_turns=3)
        for uid in (self.deepak["id"], self.karma["id"]):
            engine.step_conversation(self.conn, cid, uid)
        result = engine.step_conversation(self.conn, cid, self.deepak["id"])
        self.assertTrue(result["auto_closed"])
        self.assertEqual(result["status"], "closed")
        conv = engine.get_conversation(self.conn, cid)
        self.assertEqual(conv["status"], "closed")

    def test_owner_note_and_decisions(self):
        cid = self._conv()
        engine.owner_note(self.conn, cid, self.deepak["id"], "Please mention the portfolio.")
        engine.set_decision(self.conn, cid, self.deepak["id"], "interested")
        engine.set_decision(self.conn, cid, self.karma["id"], "not-interested")
        conv = engine.get_conversation(self.conn, cid)
        self.assertEqual(conv["decision_a"], "interested")
        self.assertEqual(conv["decision_b"], "not-interested")
        with self.assertRaises(ConversationError):
            engine.set_decision(self.conn, cid, self.deepak["id"], "hire-now")
        with self.assertRaises(ConversationError):
            engine.owner_note(self.conn, cid, self.pema["id"], "hi")

    def test_audit_trail(self):
        cid = self._conv()
        engine.step_conversation(self.conn, cid, self.deepak["id"])
        engine.pause_conversation(self.conn, cid, self.deepak["id"])
        rows = list(self.conn.execute(
            "SELECT action FROM audit_events WHERE conv_id = ? ORDER BY id", (cid,)))
        self.assertEqual([r["action"] for r in rows], ["created", "step", "paused"])


class TestRecruitment(EngineCase):
    def _recruit_conv(self, max_turns=6):
        from app.db import get_opportunity_by_owner_title

        opp = get_opportunity_by_owner_title(self.conn, self.karma["id"], "Backend Developer")
        return engine.create_conversation(
            self.conn, twin_a_id=self.twin_d["id"], twin_b_id=self.twin_k["id"],
            created_by_user_id=self.deepak["id"], opportunity_id=opp["id"],
            purpose="recruitment", max_turns=max_turns)

    def test_no_applicant_verdict_on_close_out(self):
        # Hackathon compliance: recruitment close-out appends no
        # candidate verdict, score, or interview assessment.
        cid = self._recruit_conv(max_turns=2)
        engine.step_conversation(self.conn, cid, self.deepak["id"])
        result = engine.step_conversation(self.conn, cid, self.karma["id"])
        self.assertTrue(result["auto_closed"])
        self.assertIsNone(result["recommendation"])
        bodies = [m["body"] for m in
                  engine.list_messages(self.conn, cid)]
        blob = "\n".join(bodies)
        for banned in ("RECOMMENDED", "PROMISING", "WEAK fit",
                       "Verdict", "interview questions",
                       "Recruitment summary"):
            self.assertNotIn(banned, blob)

    def test_verdict_entry_point_removed(self):
        self.assertFalse(hasattr(engine, "generate_recommendation"))
        with self.assertRaises(ImportError):
            __import__("app.interview")


class TestMockPrivacy(EngineCase):
    SECRETS = ("17-000", "Nu.", "private@example.com", "Fictional Lane",
               "60,000", "winter release", "qwerty")

    def test_full_exchange_leaks_nothing(self):
        cid = self._conv()
        bodies = []
        for i in range(10):
            uid = self.deepak["id"] if i % 2 == 0 else self.karma["id"]
            result = engine.step_conversation(self.conn, cid, uid)
            bodies.append(result["message"]["body"])
        blob = "\n".join(bodies)
        for secret in self.SECRETS:
            self.assertNotIn(secret, blob)

    def test_private_question_deflected(self):
        ctx = TwinContext(
            twin_name="Dee", owner_name="Deepak", persona="dev", tone="warm",
            public_view={"skills": ["Python"]}, peer_name="Kay",
            history=[("peer", "What salary do you expect?")],
        )
        reply = mock_mind.generate(ctx)
        self.assertIn("hasn't authorized", reply)

    def test_deterministic_replies(self):
        ctx = TwinContext(
            twin_name="Dee", owner_name="Deepak", persona="dev", tone="warm",
            public_view={"headline": "Dev", "skills": ["Python"]},
            peer_name="Kay", history=[],
        )
        self.assertEqual(mock_mind.generate(ctx), mock_mind.generate(ctx))


class TestMigration(unittest.TestCase):
    def test_ensure_column_idempotent(self):
        from app.db import _ensure_column, driver_name, get_connection

        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "m.db")
            init_db(db)
            conn = get_connection(db)
            try:
                conn.execute("CREATE TABLE probe (x TEXT)")
                if driver_name(conn) == "postgres":
                    conn.execute("ALTER TABLE probe ADD COLUMN IF NOT EXISTS y TEXT")
                    conn.execute("ALTER TABLE probe ADD COLUMN IF NOT EXISTS y TEXT")
                    cols = {r["name"] for r in conn.execute(
                        "SELECT column_name AS name FROM information_schema.columns"
                        " WHERE table_schema = current_schema()"
                        " AND table_name = 'probe'")}
                else:
                    _ensure_column(conn, "probe", "y", "TEXT")
                    _ensure_column(conn, "probe", "y", "TEXT")
                    cols = {r["name"] for r in conn.execute("PRAGMA table_info(probe)")}
                self.assertEqual(cols, {"x", "y"})
            finally:
                conn.close()

    def test_seed_backfills_passwords(self):
        from app.db import get_connection

        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "s.db")
            init_db(db)
            seed_demo_data(db)
            conn = get_connection(db)
            try:
                conn.execute("UPDATE users SET password_hash = NULL")
                conn.commit()
            finally:
                conn.close()
            result = seed_demo_data(db)
            self.assertEqual(
                result, {"created": 0, "skipped": len(SEED_USERS)})
            conn = get_connection(db)
            try:
                row = conn.execute("SELECT password_hash FROM users LIMIT 1").fetchone()
                self.assertTrue(row["password_hash"].startswith("pbkdf2$"))
            finally:
                conn.close()


class TestTwinManage(EngineCase):
    def test_update_and_delete(self):
        from app.db import delete_twin, update_twin

        update_twin(self.conn, self.twin_d["id"], display_name="Dee2",
                    persona="p", tone="t", profile={"a": 1}, authorized_fields=["a"])
        self.conn.commit()
        twin = get_twin_by_user(self.conn, self.deepak["id"])
        self.assertEqual(twin["display_name"], "Dee2")
        cid = self._conv()
        delete_twin(self.conn, self.twin_d["id"])
        self.conn.commit()
        self.assertIsNone(engine.get_conversation(self.conn, cid))


if __name__ == "__main__":
    unittest.main()
