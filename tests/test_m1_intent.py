"""M1 intent-service tests: deterministic-first, mock + Meta refinement."""

import json
import tempfile
import unittest
from pathlib import Path

from app import budget as budget_engine
from app import requests as need_engine
from app.db import (
    get_connection,
    get_twin_by_user,
    get_user_by_email,
    init_db,
)
from app.intent import (
    INTENT_SCHEMA,
    MockIntentRefiner,
    interpret_need,
    sanitize_refinement,
)
from app.meta_client import MetaAPIError, MetaProvider
from app.request_parse import parse_need
from app.seed import seed_demo_data

TAXI = ("I need a taxi from GCIT to Thimphu tomorrow at 9 AM. "
        "My maximum budget is Nu. 500.")
JOB = "Find a React developer available for a project."
SERVICE = "Find a machine-learning tutor."
SUPPLY = "Find a supplier for 500 units of notebooks."
TEAM = "Find people interested in collaborating on an AI project."
LAPTOP = "Find a laptop under $800 with 16GB RAM."

SAMPLES = (
    (TAXI, "transport"), (JOB, "job"), (SERVICE, "service"),
    (SUPPLY, "supply"), (TEAM, "collaboration"), (LAPTOP, "other"),
)


def _fake_post(payload_obj, tokens_in=600, tokens_out=250):
    def fake_post(url, key, payload):
        fake_post.seen.append(payload)
        return {"choices": [{"message": {"content": json.dumps(payload_obj)}}],
                "usage": {"prompt_tokens": tokens_in,
                          "completion_tokens": tokens_out}}
    fake_post.seen = []
    return fake_post


def _meta_provider(payload_obj, **kwargs):
    post = _fake_post(payload_obj, **kwargs)
    provider = MetaProvider(model="muse-spark-1.3", api_key="k", post=post)
    return provider, post


class M1Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "m1.db")
        init_db(self.db)
        seed_demo_data(self.db)
        self.conn = get_connection(self.db)
        self.deepak = get_user_by_email(self.conn, "deepak.demo@example.com")
        self.twin = get_twin_by_user(self.conn, self.deepak["id"])

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()


class TestSamples(unittest.TestCase):
    def test_six_samples_across_six_categories(self):
        seen = set()
        for text, category in SAMPLES:
            with self.subTest(text=text):
                out = interpret_need(text, provider=MockIntentRefiner())
                self.assertEqual(out["category"], category)
                self.assertIn(out["source"], ("deterministic", "mock-refined"))
                self.assertIsNone(out["usage"])
                seen.add(category)
        self.assertGreaterEqual(len(seen), 4)

    def test_taxi_takes_deterministic_path(self):
        out = interpret_need(TAXI, provider=MockIntentRefiner())
        self.assertEqual(out["source"], "deterministic")
        self.assertEqual(out["confidence"], "high")
        self.assertEqual(out["clarification"], "")
        self.assertEqual(out["requirements"]["budget_max"], 500.0)
        self.assertEqual(out["entity"], "ride")

    def test_high_confidence_never_calls_model(self):
        provider, post = _meta_provider({"category": "transport"})
        out = interpret_need(TAXI, provider=provider)
        self.assertEqual(post.seen, [])
        self.assertEqual(out["source"], "deterministic")

    def test_ambiguous_gets_useful_clarification(self):
        out = interpret_need("I need help", provider=MockIntentRefiner())
        self.assertEqual(out["confidence"], "low")
        self.assertIn("ride", out["clarification"])
        self.assertIn("collaborator", out["clarification"])

    def test_laptop_keeps_budget_asks_category(self):
        out = interpret_need(LAPTOP, provider=MockIntentRefiner())
        self.assertEqual(out["requirements"]["budget_max"], 800.0)
        self.assertEqual(out["requirements"]["budget_currency"], "$")
        self.assertTrue(out["clarification"])

    def test_mock_refinement_adds_no_facts(self):
        for text, _ in SAMPLES:
            with self.subTest(text=text):
                out = interpret_need(text, provider=MockIntentRefiner())
                self.assertEqual(out["requirements"],
                                 parse_need(text)["requirements"])
                self.assertEqual(out["preferences"], {})


class TestMetaRefinement(M1Case):
    def test_structured_call_shape_and_usage(self):
        payload = {"category": "job", "entity": "React developer",
                   "requirements": {"skills": ["React"]},
                   "preferences": ["availability: weekends"],
                   "needs_web_search": False, "needs_twin_search": True,
                   "needs_contact": False, "clarification": "",
                   "confidence": "high"}
        provider, post = _meta_provider(payload)
        out = interpret_need(JOB, conn=self.conn,
                             user_id=self.deepak["id"], provider=provider)
        self.assertEqual(len(post.seen), 1)
        sent = post.seen[0]
        self.assertEqual(sent["model"], "muse-spark-1.3")
        self.assertEqual(sent["response_format"]["type"], "json_schema")
        self.assertGreaterEqual(sent["max_tokens"], 1500)
        self.assertEqual(sent["response_format"]["json_schema"]["schema"],
                         INTENT_SCHEMA)
        self.assertEqual(out["source"], "meta")
        self.assertEqual(out["entity"], "React developer")
        self.assertEqual(out["requirements"]["skills"], ["React"])
        self.assertEqual(out["preferences"], {"availability": "weekends"})
        self.assertEqual(
            out["usage"],
            {"provider": "meta", "model": "muse-spark-1.3",
             "tokens_in": 600, "tokens_out": 250,
             "est_cost_usd": round(
                 budget_engine.estimate_cost("muse-spark-1.3", 600, 250), 6)})
        row = self.conn.execute(
            "SELECT provider, model, tokens_in, tokens_out, est_cost_usd"
            " FROM usage_log").fetchone()
        self.assertEqual(row["provider"], "meta")
        self.assertEqual(row["tokens_in"], 600)
        self.assertGreater(row["est_cost_usd"], 0)

    def test_deterministic_values_win_conflicts(self):
        payload = {"category": "supply", "entity": "cheap laptop",
                   "requirements": {"budget_max": 9999.0,
                                    "budget_currency": "$",
                                    "skills": ["gaming"]},
                   "needs_twin_search": False, "clarification": "x",
                   "confidence": "high"}
        provider, _ = _meta_provider(payload)
        out = interpret_need(LAPTOP, conn=self.conn,
                             user_id=self.deepak["id"], provider=provider)
        # Known deterministic values survive; the model only fills gaps
        # (unknown category) and missing slots (skills).
        self.assertEqual(out["requirements"]["budget_max"], 800.0)
        self.assertEqual(out["category"], "supply")
        self.assertEqual(out["requirements"]["skills"], ["gaming"])

    def test_known_category_survives_model_override(self):
        payload = {"category": "supply", "entity": "dev shop",
                   "requirements": {"skills": ["React"]},
                   "needs_twin_search": True, "clarification": "",
                   "confidence": "high"}
        provider, _ = _meta_provider(payload)
        out = interpret_need(JOB, conn=self.conn,
                             user_id=self.deepak["id"], provider=provider)
        self.assertEqual(out["category"], "job")
        self.assertEqual(out["requirements"]["skills"], ["React"])

    def test_invalid_category_sanitized(self):
        det = parse_need("I need help")
        clean = sanitize_refinement({"category": "spaceship",
                                     "requirements": {}}, det)
        self.assertEqual(clean["category"], "other")

    def test_prompt_contains_only_need_and_preparse(self):
        provider, post = _meta_provider({"category": "job"})
        interpret_need(JOB, conn=self.conn, user_id=self.deepak["id"],
                       provider=provider)
        blob = json.dumps(post.seen[0])
        self.assertIn(JOB, blob)
        self.assertNotIn("memories", blob)
        self.assertNotIn("phone", blob)
        self.assertNotIn("password", blob)

    def test_model_failure_falls_back_to_mock(self):
        def boom(url, key, payload):
            raise MetaAPIError("simulated outage")
        provider = MetaProvider(model="muse-spark-1.3", api_key="k",
                                post=boom)
        out = interpret_need(JOB, conn=self.conn,
                             user_id=self.deepak["id"], provider=provider)
        self.assertEqual(out["source"], "mock-refined")
        self.assertTrue(any("unavailable" in w for w in out["warnings"]))
        # The attempt may have been billed: it must leave a labelled row,
        # never vanish and never pose as a success.
        rows = self.conn.execute(
            "SELECT status, notes, est_cost_usd FROM usage_log").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "failed_unknown")
        self.assertIn("simulated outage", rows[0]["notes"])

    def test_unconfigured_model_falls_back_without_call(self):
        seen = []

        def boom(url, key, payload):
            seen.append(payload)
            raise AssertionError("must not be called")
        provider = MetaProvider(model="muse-spark-1.3", api_key="",
                                post=boom)
        out = interpret_need(JOB, conn=self.conn,
                             user_id=self.deepak["id"], provider=provider)
        self.assertEqual(seen, [])
        self.assertEqual(out["source"], "mock-refined")

    def test_budget_exhaustion_falls_back_without_call(self):
        budget_engine.record_usage(
            self.conn, provider="meta", model="muse-spark-1.3", conv_id=None,
            user_id=self.deepak["id"], tokens_in=4_000_001, tokens_out=0)
        provider, post = _meta_provider({"category": "job"})
        out = interpret_need(JOB, conn=self.conn,
                             user_id=self.deepak["id"], provider=provider)
        self.assertEqual(post.seen, [])
        self.assertEqual(out["source"], "mock-refined")

    def test_no_connection_no_spend(self):
        provider, post = _meta_provider({"category": "job"})
        out = interpret_need(JOB, provider=provider)
        self.assertEqual(post.seen, [])
        self.assertEqual(out["source"], "mock-refined")
        self.assertTrue(any("usage-recording" in w for w in out["warnings"]))


class TestFailureAccounting(M1Case):
    def _run(self, post):
        provider = MetaProvider(model="muse-spark-1.3", api_key="k",
                                post=post)
        return interpret_need(JOB, conn=self.conn,
                              user_id=self.deepak["id"], provider=provider)

    def _rows(self):
        return self.conn.execute(
            "SELECT provider, status, tokens_in, tokens_out, est_cost_usd,"
            " notes FROM usage_log ORDER BY id").fetchall()

    def test_starvation_shape_falls_back_with_known_cost(self):
        # Exact shape observed live: reasoning exhausted max_tokens,
        # finish_reason "length", null content, usage still reported.
        def starved(url, key, payload):
            return {"choices": [{"finish_reason": "length",
                                 "message": {"content": None,
                                             "refusal": None}}],
                    "usage": {"prompt_tokens": 366,
                              "completion_tokens": 600,
                              "completion_tokens_details":
                                  {"reasoning_tokens": 598}}}
        out = self._run(starved)
        self.assertNotEqual(out["source"], "meta")
        self.assertEqual(out["source"], "mock-refined")
        self.assertEqual(out["category"], "job")  # deterministic survives
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "failed_known")
        self.assertEqual(rows[0]["tokens_in"], 366)
        self.assertEqual(rows[0]["tokens_out"], 600)
        self.assertAlmostEqual(
            rows[0]["est_cost_usd"],
            budget_engine.estimate_cost("muse-spark-1.3", 366, 600))
        self.assertIn("empty reply", rows[0]["notes"])

    def test_invalid_json_with_usage_is_failed_known(self):
        def garbage(url, key, payload):
            return {"choices": [{"message": {"content": "not json"}}],
                    "usage": {"prompt_tokens": 100,
                              "completion_tokens": 10}}
        out = self._run(garbage)
        self.assertEqual(out["source"], "mock-refined")
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "failed_known")
        self.assertEqual(rows[0]["tokens_out"], 10)
        self.assertGreater(rows[0]["est_cost_usd"], 0)

    def test_non_object_json_is_failed_known(self):
        def array(url, key, payload):
            return {"choices": [{"message": {"content": "[1, 2]"}}],
                    "usage": {"prompt_tokens": 50,
                              "completion_tokens": 5}}
        out = self._run(array)
        self.assertEqual(out["source"], "mock-refined")
        self.assertEqual(self._rows()[0]["status"], "failed_known")

    def test_missing_usage_block_is_failed_unknown(self):
        def no_usage(url, key, payload):
            return {"choices": [{"message": {"content": '{"a": 1}'}}],
                    "usage": {}}
        out = self._run(no_usage)
        self.assertEqual(out["source"], "mock-refined")
        rows = self._rows()
        self.assertEqual(rows[0]["status"], "failed_unknown")
        self.assertTrue(rows[0]["notes"])

    def test_timeout_is_failed_unknown(self):
        def slow(url, key, payload):
            raise TimeoutError("simulated timeout")
        out = self._run(slow)
        self.assertEqual(out["source"], "mock-refined")
        self.assertEqual(out["requirements"], {})
        rows = self._rows()
        self.assertEqual(rows[0]["status"], "failed_unknown")
        self.assertIn("timeout", rows[0]["notes"].lower())

    def test_os_error_is_failed_unknown(self):
        def down(url, key, payload):
            raise OSError("simulated network down")
        out = self._run(down)
        self.assertEqual(out["source"], "mock-refined")
        self.assertEqual(self._rows()[0]["status"], "failed_unknown")

    def test_unknown_usage_never_silent_zero(self):
        self._run(lambda u, k, p: (_ for _ in ()).throw(
            MetaAPIError("boom")))
        row = self._rows()[0]
        self.assertEqual(row["est_cost_usd"], 0)
        self.assertEqual(row["status"], "failed_unknown")
        self.assertTrue(row["notes"])
        summary = budget_engine.usage_summary(self.conn)
        statuses = {s["status"]: s for s in summary["by_status"]}
        self.assertIn("failed_unknown", statuses)
        self.assertEqual(statuses["failed_unknown"]["requests"], 1)

    def test_summary_distinguishes_four_outcomes(self):
        budget_engine.record_usage(
            self.conn, provider="mock", model="mock", conv_id=None,
            user_id=self.deepak["id"], tokens_in=0, tokens_out=0)
        budget_engine.record_usage(
            self.conn, provider="meta", model="muse-spark-1.3", conv_id=None,
            user_id=self.deepak["id"], tokens_in=10, tokens_out=5)
        budget_engine.record_usage(
            self.conn, provider="meta", model="muse-spark-1.3", conv_id=None,
            user_id=self.deepak["id"], tokens_in=10, tokens_out=5,
            status="failed_known", notes="bad json")
        budget_engine.record_usage(
            self.conn, provider="meta", model="muse-spark-1.3", conv_id=None,
            user_id=self.deepak["id"], tokens_in=0, tokens_out=0,
            status="failed_unknown", notes="timeout")
        summary = budget_engine.usage_summary(self.conn)
        statuses = {s["status"]: s["requests"] for s in summary["by_status"]}
        self.assertEqual(statuses,
                         {"failed_known": 1, "failed_unknown": 1, "ok": 2})

    def test_failed_known_cost_counts_toward_guard(self):
        budget_engine.record_usage(
            self.conn, provider="meta", model="muse-spark-1.3", conv_id=None,
            user_id=self.deepak["id"], tokens_in=4_000_001, tokens_out=0,
            status="failed_known", notes="empty reply")
        seen = []

        def counting(url, key, payload):
            seen.append(payload)
            raise AssertionError("must not be called")
        self._run(counting)
        self.assertEqual(seen, [])

    def test_unsent_calls_leave_no_row(self):
        no_key = MetaProvider(model="muse-spark-1.3", api_key="",
                              post=lambda u, k, p: {})
        interpret_need(JOB, conn=self.conn, user_id=self.deepak["id"],
                       provider=no_key)
        self.assertEqual(self._rows(), [])


class TestOwnerCorrection(M1Case):
    def test_interpret_never_mutates_stored_request(self):
        req_id, _ = need_engine.create_need(
            self.conn, twin_id=self.twin["id"],
            owner_user_id=self.deepak["id"], category="other",
            need_text=LAPTOP, requirements=parse_need(LAPTOP)["requirements"],
            idempotency_key="m1-laptop")
        interpret_need(LAPTOP, provider=MockIntentRefiner())
        stored = need_engine.get_request(self.conn, req_id)
        self.assertEqual(stored["category"], "other")
        self.assertEqual(need_engine.request_requirements(stored),
                         parse_need(LAPTOP)["requirements"])
        confirmed = need_engine.confirm_request(
            self.conn, req_id, self.deepak["id"], category="supply",
            requirements={"budget_max": 800.0})
        self.assertEqual(confirmed["category"], "supply")
        self.assertEqual(confirmed["status"], "open")


if __name__ == "__main__":
    unittest.main()
