"""Phase 3B maintained tests: budgets, verified client, fallback (no network)."""

import tempfile
import unittest
from pathlib import Path

from app import budget
from app.budget import BudgetExhausted
from app.conversations import TwinContext
from app.db import get_twin_by_user, get_user_by_email, init_db
from app.meta_client import (
    API_BASE,
    CHAT_PATH,
    MetaAPIError,
    MetaNotConfigured,
    MetaProvider,
    build_messages,
)
from app.seed import seed_demo_data


def _ctx(**kwargs):
    base = {"twin_name": "Dee", "owner_name": "Deepak", "persona": "dev",
            "tone": "warm", "public_view": {"skills": ["Python"]},
            "peer_name": "Kay"}
    base.update(kwargs)
    return TwinContext(**base)


class BudgetCase(unittest.TestCase):
    def setUp(self):
        from app.db import get_connection

        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "t3b.db")
        init_db(self.db)
        seed_demo_data(self.db)
        self.conn = get_connection(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()


class TestPricing(unittest.TestCase):
    def test_spark_math(self):
        cost = budget.estimate_cost("muse-spark-1.3", 1000, 500)
        self.assertAlmostEqual(cost, 0.001 * 1.25 + 0.0005 * 4.25)

    def test_unknown_model_refused(self):
        with self.assertRaises(BudgetExhausted):
            budget.estimate_cost("muse-mystery-9", 10, 10)


class TestCaps(BudgetCase):
    def _spend(self, amount, user_id=1, conv_id=1):
        self.conn.execute(
            "INSERT INTO usage_log (provider, model, conv_id, user_id,"
            " tokens_in, tokens_out, est_cost_usd) VALUES ('meta', 'muse-spark-1.3',"
            " ?, ?, 0, 0, ?)",
            (conv_id, user_id, amount),
        )
        self.conn.commit()

    def test_monthly_cap(self):
        self._spend(80.0)
        with self.assertRaises(BudgetExhausted):
            budget.check_budgets(self.conn, 1, 1)

    def test_user_daily_cap(self):
        self._spend(6.0, user_id=2, conv_id=9)
        with self.assertRaises(BudgetExhausted):
            budget.check_budgets(self.conn, 2, 1)
        budget.check_budgets(self.conn, 3, 1)  # other user unaffected

    def test_conversation_cap(self):
        self._spend(1.5, conv_id=4)
        with self.assertRaises(BudgetExhausted):
            budget.check_budgets(self.conn, 1, 4)
        budget.check_budgets(self.conn, 1, 5)

    def test_record_and_summary(self):
        self.conn.execute("DELETE FROM usage_log")
        self.conn.commit()
        cost = budget.record_usage(self.conn, provider="meta", model="muse-spark-1.3",
                                   conv_id=1, user_id=1, tokens_in=1000, tokens_out=500)
        self.assertAlmostEqual(cost, 0.003375)
        summary = budget.usage_summary(self.conn)
        self.assertEqual(summary["requests"], 1)
        self.assertEqual(summary["tokens_in"], 1000)
        self.assertAlmostEqual(summary["est_cost_usd"], 0.003375)

    def test_mock_records_zero(self):
        from app import conversations as engine
        from app.providers import MockProvider

        deepak = get_user_by_email(self.conn, "deepak.demo@example.com")
        karma = get_user_by_email(self.conn, "karma.demo@example.com")
        twin_d = get_twin_by_user(self.conn, deepak["id"])
        twin_k = get_twin_by_user(self.conn, karma["id"])
        cid = engine.create_conversation(
            self.conn, twin_a_id=twin_d["id"], twin_b_id=twin_k["id"],
            created_by_user_id=deepak["id"])
        # Explicit mock provider: independent of ambient AI_PROVIDER.
        engine.step_conversation(self.conn, cid, deepak["id"],
                                 provider=MockProvider())
        row = self.conn.execute("SELECT * FROM usage_log").fetchone()
        self.assertEqual(row["provider"], "mock")
        self.assertEqual(row["est_cost_usd"], 0.0)


class TestMetaClient(unittest.TestCase):
    def test_endpoint_verified_constant(self):
        self.assertEqual(API_BASE + CHAT_PATH,
                         "https://api.meta.ai/v1/chat/completions")

    def test_refuses_unconfigured_without_network(self):
        calls = []

        def fake_post(url, key, payload):
            calls.append(url)
            return {}

        cases = [{"model": "", "api_key": "k"},
                 {"model": "muse-spark-1.3-contributor", "api_key": "k"},
                 {"model": "muse-nope-0", "api_key": "k"},
                 {"model": "muse-spark-1.3", "api_key": ""}]
        for kwargs in cases:
            with self.assertRaises(MetaNotConfigured, msg=kwargs):
                MetaProvider(post=fake_post, **kwargs).generate(_ctx())
        self.assertEqual(calls, [])

    def test_parses_verified_shape(self):
        def fake_post(url, key, payload):
            self.assertEqual(payload["model"], "muse-spark-1.3")
            self.assertEqual(payload["max_tokens"], 1500)
            self.assertEqual(payload["messages"][0]["role"], "system")
            return {"choices": [{"message": {"content": "  Hello twin!  "}}],
                    "usage": {"prompt_tokens": 40, "completion_tokens": 5}}

        result = MetaProvider(model="muse-spark-1.3", api_key="k",
                              post=fake_post).generate(_ctx())
        self.assertEqual(result.text, "Hello twin!")
        self.assertEqual((result.tokens_in, result.tokens_out), (40, 5))

    def test_diagnostics_captured_when_reported(self):
        def detailed(url, key, payload):
            return {"choices": [{"finish_reason": "stop",
                                 "message": {"content": "Hi."}}],
                    "usage": {"prompt_tokens": 10,
                              "completion_tokens": 1150,
                              "completion_tokens_details": {
                                  "reasoning_tokens": 1110}}}

        result = MetaProvider(model="muse-spark-1.3", api_key="k",
                              post=detailed).generate(_ctx())
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(result.reasoning_tokens, 1110)

        def bare(url, key, payload):
            return {"choices": [{"message": {"content": "Hi."}}],
                    "usage": {"prompt_tokens": 10,
                              "completion_tokens": 2}}

        result = MetaProvider(model="muse-spark-1.3", api_key="k",
                              post=bare).generate(_ctx())
        self.assertIsNone(result.finish_reason)
        self.assertIsNone(result.reasoning_tokens)

    def test_generate_override_reaches_payload(self):
        seen = []

        def fake_post(url, key, payload):
            seen.append(payload)
            return {"choices": [{"message": {"content": "Hi."}}],
                    "usage": {"prompt_tokens": 10,
                              "completion_tokens": 2}}

        provider = MetaProvider(model="muse-spark-1.3", api_key="k",
                                post=fake_post)
        provider.generate(_ctx(), max_tokens=1500)
        self.assertEqual(seen[0]["max_tokens"], 1500)
        for bad in (0, -1, True, 1501, "300"):
            with self.subTest(limit=bad), self.assertRaises(ValueError):
                provider.generate(_ctx(), max_tokens=bad)
        self.assertEqual(len(seen), 1)

    def test_truncation_detected_explicitly(self):
        def starved(url, key, payload):
            return {"choices": [{"finish_reason": "length",
                                 "message": {"content": None}}],
                    "usage": {"prompt_tokens": 200,
                              "completion_tokens": 300}}

        provider = MetaProvider(model="muse-spark-1.3", api_key="k",
                                post=starved)
        with self.assertRaises(MetaAPIError) as ctx:
            provider.generate(_ctx())
        self.assertIn("empty reply", str(ctx.exception))
        self.assertIn("truncated", str(ctx.exception))
        self.assertEqual((ctx.exception.tokens_in,
                          ctx.exception.tokens_out), (200, 300))

        def partial(url, key, payload):
            return {"choices": [{"finish_reason": "length",
                                 "message": {"content": "Half a"}}],
                    "usage": {"prompt_tokens": 200,
                              "completion_tokens": 300}}

        provider = MetaProvider(model="muse-spark-1.3", api_key="k",
                                post=partial)
        with self.assertRaises(MetaAPIError) as ctx:
            provider.generate(_ctx())
        self.assertIn("truncated", str(ctx.exception))

    def test_http_error_carries_server_detail(self):
        import io
        import urllib.error
        from unittest import mock

        from app import meta_client as meta_mod

        def failing(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 400, "Bad Request", {},
                io.BytesIO(b'{"error": "bad schema"}'))

        with mock.patch.object(meta_mod.urllib.request, "urlopen",
                               side_effect=failing):
            with self.assertRaises(MetaAPIError) as ctx:
                meta_mod._urllib_post("https://api.meta.ai/v1/x", "k",
                                      {"a": 1})
        self.assertIn("HTTP 400", str(ctx.exception))
        self.assertIn("bad schema", str(ctx.exception))
        self.assertFalse(ctx.exception.usage_known)

    def test_rejects_bad_shapes(self):
        for bad in ({}, {"choices": []}, {"choices": [{"message": {"content": "  "}}]}):
            def fake_post(url, key, payload, _bad=bad):
                return _bad

            with self.assertRaises(MetaAPIError):
                MetaProvider(model="muse-spark-1.3", api_key="k",
                             post=fake_post).generate(_ctx())

    def test_build_messages_bounded(self):
        ctx = _ctx(history=[("peer", "q%d?" % i) for i in range(20)],
                   memories=["m1", "m2", "m3", "m4"])
        messages = build_messages(ctx)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(len(messages), 1 + 6)  # system + last 6 turns
        self.assertIn("Python", messages[0]["content"])
        self.assertNotIn("m4", messages[0]["content"])  # at most 3 memories

    def test_opening_turn_has_user_message(self):
        ctx = _ctx(history=[],
                   opportunity={"title": "Backend Developer"})
        messages = build_messages(ctx)
        roles = [m["role"] for m in messages]
        self.assertEqual(roles, ["system", "user"])
        self.assertIn("Backend Developer", messages[1]["content"])
        self.assertIn("Kay", messages[1]["content"])
        blob = " ".join(m["content"] for m in messages)
        self.assertNotIn("budget_max", blob)

    def test_later_turns_unchanged(self):
        ctx = _ctx(history=[("peer", "Hello there"),
                            ("me", "Hi back"),
                            ("peer", "Know Python?")])
        messages = build_messages(ctx)
        self.assertEqual(
            [(m["role"], m["content"]) for m in messages],
            [("system", messages[0]["content"]),
             ("user", "Hello there"),
             ("assistant", "Hi back"),
             ("user", "Know Python?")])


class TestFallback(BudgetCase):
    def _conv(self):
        from app import conversations as engine

        deepak = get_user_by_email(self.conn, "deepak.demo@example.com")
        karma = get_user_by_email(self.conn, "karma.demo@example.com")
        twin_d = get_twin_by_user(self.conn, deepak["id"])
        twin_k = get_twin_by_user(self.conn, karma["id"])
        cid = engine.create_conversation(
            self.conn, twin_a_id=twin_d["id"], twin_b_id=twin_k["id"],
            created_by_user_id=deepak["id"])
        return engine, cid, deepak["id"]

    def test_transport_failure_falls_back_with_notice(self):
        from app import conversations as engine

        def boom(url, key, payload):
            raise MetaAPIError("simulated outage")

        engine_mod, cid, uid = self._conv()
        provider = MetaProvider(model="muse-spark-1.3", api_key="k", post=boom)
        result = engine_mod.step_conversation(self.conn, cid, uid, provider=provider)
        self.assertEqual(result["message"]["provider"], "mock")
        self.assertIn("Hi Kay!", result["message"]["body"])
        bodies = [m["body"] for m in engine.list_messages(self.conn, cid)]
        self.assertTrue(any("Meta inference unavailable" in b for b in bodies))
        actions = [r["action"] for r in self.conn.execute(
            "SELECT action FROM audit_events WHERE conv_id = ?", (cid,))]
        self.assertIn("budget-fallback", actions)

    def test_exhausted_budget_falls_back(self):
        from app import conversations as engine

        engine_mod, cid, uid = self._conv()
        self.conn.execute(
            "INSERT INTO usage_log (provider, model, conv_id, user_id,"
            " tokens_in, tokens_out, est_cost_usd)"
            " VALUES ('meta', 'muse-spark-1.3', ?, ?, 0, 0, 5.0)",
            (cid, uid),
        )
        self.conn.commit()
        calls = []

        def counting_post(url, key, payload):
            calls.append(url)
            return {"choices": [{"message": {"content": "paid"}}], "usage": {}}

        provider = MetaProvider(model="muse-spark-1.3", api_key="k", post=counting_post)
        result = engine_mod.step_conversation(self.conn, cid, uid, provider=provider)
        self.assertEqual(result["message"]["provider"], "mock")
        self.assertEqual(calls, [])  # budget checked BEFORE any network attempt

    def test_tokens_recorded_on_message(self):
        from app import conversations as engine

        def fake_post(url, key, payload):
            return {"choices": [{"message": {"content": "Live reply."}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 3}}

        engine_mod, cid, uid = self._conv()
        provider = MetaProvider(model="muse-spark-1.3", api_key="k", post=fake_post)
        result = engine_mod.step_conversation(self.conn, cid, uid, provider=provider)
        self.assertEqual(result["message"]["provider"], "meta")
        self.assertEqual(result["message"]["tokens_in"], 10)
        self.assertEqual(result["message"]["tokens_out"], 3)


if __name__ == "__main__":
    unittest.main()
