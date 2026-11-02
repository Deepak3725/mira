"""Offline tests: eval harness (scenarios, gating, metrics).

No provider calls: Meta paths use injected fake transports, the eval
live path is exercised only with a stubbed provider class, and
dry-run mode is proven to build no provider at all.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import conversations as engine
from app.db import (
    create_twin,
    create_user,
    get_connection,
    init_db,
)
from app.eval import runner as eval_runner
from app.eval import scenarios as eval_scenarios
from app.eval import synth
from app.meta_client import MetaProvider
# NOTE (hackathon compliance): recruitment-pair fixture removed with
# app/interview.py; MockProvider/adapter imports unused after that.


class InterviewCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "iv.db")
        init_db(self.db)
        self.conn = get_connection(self.db)
        self.addCleanup(self.conn.close)


# NOTE (hackathon compliance): interview question tests removed with app/interview.py.


class TestChatCapPlumbing(InterviewCase):
    def test_step_conversation_passes_cap(self):
        a = create_user(self.conn, name="A", role="professional",
                        email="a@cap.test", password_hash=None)
        b = create_user(self.conn, name="B", role="professional",
                        email="b@cap.test", password_hash=None)
        ta = create_twin(self.conn, user_id=a, display_name="AT",
                         persona="p", tone="warm", profile={},
                         authorized_fields=[], avatar_seed="a")
        tb = create_twin(self.conn, user_id=b, display_name="BT",
                         persona="p", tone="warm", profile={},
                         authorized_fields=[], avatar_seed="b")
        cid = engine.create_conversation(
            self.conn, twin_a_id=ta, twin_b_id=tb,
            created_by_user_id=a)
        seen = []

        def fake(url, key, payload):
            seen.append(payload)
            return {"choices": [{"message": {"content": "Hi."}}],
                    "usage": {"prompt_tokens": 10,
                              "completion_tokens": 2}}

        provider = MetaProvider(model="muse-spark-1.3", api_key="k",
                                post=fake)
        engine.step_conversation(self.conn, cid, a, provider=provider,
                                 max_tokens=1500)
        self.assertEqual(seen[0]["max_tokens"], 1500)
        engine.step_conversation(self.conn, cid, b, provider=provider)
        self.assertEqual(seen[1]["max_tokens"], 1500)


class TestSynth(unittest.TestCase):
    def test_deterministic_and_private_marked(self):
        first = synth.synthetic_twins(3, seed=7)
        second = synth.synthetic_twins(3, seed=7)
        self.assertEqual(first, second)
        self.assertNotEqual(first, synth.synthetic_twins(3, seed=8))
        self.assertIn("phone", first[0]["profile"])
        self.assertNotIn("phone", first[0]["authorized_fields"])

    def test_leak_scan(self):
        profile = {"phone": "+975-17-SYN-001",
                   "personal_email": "a@b.invalid",
                   "salary_expectation": "Nu. 1/month",
                   "home_address": "1 Lane"}
        self.assertEqual(
            eval_scenarios.leaked_markers("call +975-17-SYN-001 now",
                                          profile),
            ["phone"])
        self.assertEqual(
            eval_scenarios.leaked_markers("I know Python well.", profile),
            [])

    def test_fallback_counting_split_sources(self):
        fell_back = eval_runner._case_fell_back
        self.assertFalse(fell_back("intent", {"source": "meta"}, True))
        self.assertTrue(
            fell_back("intent", {"source": "mock-refined"}, True))
        self.assertFalse(fell_back(
            "single_turn", {"providers": ["meta"]}, True))
        self.assertTrue(fell_back(
            "single_turn", {"providers": ["meta", "mock"]}, True))
        self.assertFalse(fell_back(
            "single_turn", {"providers": ["mock"]}, False))

    def test_on_topic_heuristic(self):
        self.assertEqual(
            eval_scenarios.on_topic_replies(
                ["I love Python programming",
                 "Python projects teach a lot"]),
            1)
        self.assertEqual(
            eval_scenarios.on_topic_replies(
                ["I love Python programming",
                 "The weather is nice today"]),
            0)




class EvalRunnerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "eval.db")

    def _args(self, *extra):
        return ["--db-path", self.db, "--cases", "1", "--turns", "2",
                *extra]


class TestEvalDryRun(EvalRunnerCase):
    def test_dry_run_plans_without_calls(self):
        # Dry-run must perform no network I/O. Deterministic offline
        # checks still run against scratch DBs with fake transports,
        # so the guard sits at the network layer, not the engine.
        with mock.patch(
                "app.meta_client._urllib_post",
                side_effect=AssertionError("no network in dry-run")), \
                mock.patch(
                    "app.ai.adapters.base.http_post_json",
                    side_effect=AssertionError(
                        "no network in dry-run")):
            summary = eval_runner.main(self._args(
                "--dry-run", "--scenarios",
                "single_turn,intent,match_explanation"))
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["mode"], "dry-run")
        self.assertGreater(summary["planned_requests"], 0)
        self.assertGreater(summary["est_cost_usd_upper_bound"], 0.0)
        conn = get_connection(self.db)
        try:
            usage = list(conn.execute("SELECT * FROM usage_log"))
            cases = list(conn.execute("SELECT * FROM eval_cases"))
        finally:
            conn.close()
        self.assertEqual(usage, [])
        self.assertTrue(cases)

    def test_unknown_scenario_refused(self):
        summary = eval_runner.main(self._args(
            "--dry-run", "--scenarios", "nope"))
        self.assertEqual(summary["status"], "refused")

    def test_multi_turn_dry_run_plans_per_turn_calls(self):
        summary = eval_runner.main(self._args(
            "--dry-run", "--scenarios", "multi_turn", "--turns", "4"))
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["planned_requests"], 4)
        self.assertGreater(summary["est_cost_usd_upper_bound"], 0.0)
        self.assertIn("multi_turn",
                      summary["by_scenario"])

    def test_chat_max_tokens_validated(self):
        for bad in ("0", "-5", "1501", "99999"):
            summary = eval_runner.main(self._args(
                "--dry-run", "--chat-max-tokens", bad))
            self.assertEqual(summary["status"], "refused",
                             msg=f"cap={bad}")

    def test_dry_estimate_uses_chat_cap(self):
        small = eval_runner.main(self._args(
            "--dry-run", "--scenarios", "single_turn",
            "--chat-max-tokens", "300"))
        big = eval_runner.main(self._args(
            "--dry-run", "--scenarios", "single_turn",
            "--chat-max-tokens", "1500"))
        self.assertEqual(small["status"], "complete")
        self.assertEqual(big["status"], "complete")
        # The worst-case bound is the unknown allowance for both, so
        # the cap moves the success component, not the headline bound.
        self.assertEqual(big["est_cost_usd_upper_bound"],
                         small["est_cost_usd_upper_bound"])
        from app.db import get_connection

        conn = get_connection(self.db)
        try:
            rows = list(conn.execute(
                "SELECT metrics_json FROM eval_cases"
                " WHERE scenario='single_turn' ORDER BY id"))
        finally:
            conn.close()
        success = [json.loads(r["metrics_json"])["est_success_usd"]
                   for r in rows]
        self.assertGreater(success[1], success[0])

    def test_app_db_path_refused(self):
        from app.db import db_path as app_db

        summary = eval_runner.main(
            ["--db-path", str(app_db()), "--cases", "1", "--dry-run"])
        self.assertEqual(summary["status"], "refused")


class TestEvalLiveGating(EvalRunnerCase):
    def test_live_without_env_refused_before_db(self):
        env = {k: v for k, v in os.environ.items()
               if k != "MIRA_EVAL_LIVE"}
        with mock.patch.dict(os.environ, env, clear=True):
            summary = eval_runner.main(self._args(
                "--live", "--max-cost-usd", "1.00"))
        self.assertEqual(summary["status"], "refused")
        self.assertFalse(Path(self.db).exists())

    def test_live_without_cost_cap_refused(self):
        with mock.patch.dict(os.environ, {"MIRA_EVAL_LIVE": "1"}):
            summary = eval_runner.main(self._args("--live"))
        self.assertEqual(summary["status"], "refused")

    def test_pre_run_gate_blocks_over_cap_plan(self):
        with mock.patch.dict(os.environ, {"MIRA_EVAL_LIVE": "1"}), \
                mock.patch("app.eval.runner.settings") as cfg:
            cfg.meta_model = "muse-spark-1.3"
            cfg.model_api_key = "k"
            # 1 request worst-cases above $0.04: a $0.01 cap refuses
            # before any database or provider work happens.
            summary = eval_runner.main(self._args(
                "--live", "--max-cost-usd", "0.01",
                "--scenarios", "single_turn"))
        self.assertEqual(summary["status"], "refused")
        self.assertIn("worst-case", summary["reason"])
        self.assertFalse(Path(self.db).exists())

    def test_summary_splits_reported_and_estimated(self):
        from app.db import get_connection, init_db

        init_db(self.db)
        conn = get_connection(self.db)
        try:
            from app import budget as budget_engine

            budget_engine.record_usage(
                conn, provider="meta", model="muse-spark-1.3",
                conv_id=None, user_id=None, tokens_in=10,
                tokens_out=5)
            budget_engine.record_usage(
                conn, provider="meta", model="muse-spark-1.3",
                conv_id=None, user_id=None, tokens_in=0, tokens_out=0,
                status="failed_unknown", notes="x")
        finally:
            conn.close()
        summary = eval_runner.main(self._args(
            "--dry-run", "--scenarios", "match_explanation"))
        # Run-scoped: pre-existing rows belong to no run.
        self.assertEqual(summary["spend_usd"], 0.0)
        self.assertIn("not the provider", summary["ledger_note"])
        # Split math over cumulative data, directly.
        conn = get_connection(self.db)
        try:
            full = eval_runner._summarize(
                conn, summary["run_id"], None)
        finally:
            conn.close()
        self.assertGreater(full["spend_reported_usd"], 0.0)
        self.assertGreater(full["spend_estimated_usd"], 0.0)
        self.assertAlmostEqual(
            full["spend_usd"],
            full["spend_reported_usd"] + full["spend_estimated_usd"])

    def test_offline_scenarios_all_pass(self):
        from app.eval import scenarios as S

        for offline in S.OFFLINE_SCENARIOS:
            results = offline()
            self.assertTrue(results, msg=offline.__name__)
            for case in results:
                self.assertTrue(
                    case.ok,
                    msg=f"{offline.__name__}: {case.metrics}")

    def test_percentile_and_reasoning_metrics(self):
        from app.db import get_connection, init_db

        init_db(self.db)
        conn = get_connection(self.db)
        try:
            for stmt in eval_runner._EVAL_TABLES:
                conn.execute(stmt)
            conn.commit()
        finally:
            conn.close()
        conn = get_connection(self.db)
        try:
            from app import budget as budget_engine

            conn.execute(
                "INSERT INTO eval_runs(started_at, mode, seed, status)"
                " VALUES ('t', 'live', 0, 'complete')")
            run_id = conn.execute(
                "SELECT last_insert_rowid()").fetchone()[0]
            budget_engine.record_usage(
                conn, provider="meta", model="muse-spark-1.3",
                conv_id=None, user_id=None, tokens_in=10,
                tokens_out=100, notes="reasoning_tokens=90")
            budget_engine.record_usage(
                conn, provider="meta", model="muse-spark-1.3",
                conv_id=None, user_id=None, tokens_in=10,
                tokens_out=50, notes="reasoning_tokens=40")
        finally:
            conn.close()
        import argparse as _argparse

        args = _argparse.Namespace(live=True)
        ctx = eval_runner.Ctx(get_connection(self.db), args, run_id)
        ctx.usage_start_id = 0
        ctx.latencies.extend([100.0, 200.0, 300.0, 400.0])
        try:
            summary = eval_runner._summarize(ctx.conn, run_id, ctx)
        finally:
            ctx.conn.close()
        self.assertEqual(summary["reasoning_tokens_total"], 130)
        self.assertIsNone(summary["cached_tokens_total"])
        self.assertEqual(summary["p50_latency_ms"], 300.0)
        self.assertEqual(summary["p95_latency_ms"], 400.0)

    def test_live_intent_fallback_fails(self):
        from app.meta_client import MetaAPIError, MetaProvider

        class StarvedProvider(MetaProvider):
            name = "meta"
            model = "muse-spark-1.3"

            def __init__(self, *args, **kwargs):
                pass

            def generate_json(self, messages, json_schema,
                              max_tokens=2000):
                raise MetaAPIError(
                    "Meta API returned an empty reply. (truncated:"
                    " finish_reason length)",
                    tokens_in=373, tokens_out=2000)

        with mock.patch.dict(os.environ, {"MIRA_EVAL_LIVE": "1"}), \
                mock.patch("app.eval.runner.settings") as cfg, \
                mock.patch("app.meta_client.MetaProvider",
                           StarvedProvider):
            cfg.meta_model = "muse-spark-1.3"
            cfg.model_api_key = "k"
            cfg.budget_monthly_usd = 75.0
            cfg.budget_per_conversation_usd = 1.0
            cfg.budget_per_user_daily_usd = 5.0
            summary = eval_runner.main(self._args(
                "--live", "--max-cost-usd", "1.00",
                "--scenarios", "intent"))
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(
            summary["by_scenario"]["intent"]["ok"], 0)
        conn = get_connection(self.db)
        try:
            rows = list(conn.execute(
                "SELECT metrics_json FROM eval_cases"
                " WHERE scenario = 'intent'"))
        finally:
            conn.close()
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(
                json.loads(row["metrics_json"])["source"],
                "mock-refined")

    def test_live_privacy_probe_mock_substitution_fails(self):
        from app.meta_client import MetaAPIError, MetaProvider

        class EmptyProvider(MetaProvider):
            name = "meta"
            model = "muse-spark-1.3"

            def __init__(self, *args, **kwargs):
                pass

            def generate(self, context, max_tokens=None):
                raise MetaAPIError("Meta API returned an empty reply.",
                                   tokens_in=10, tokens_out=5)

        with mock.patch.dict(os.environ, {"MIRA_EVAL_LIVE": "1"}), \
                mock.patch("app.eval.runner.settings") as cfg, \
                mock.patch("app.meta_client.MetaProvider",
                           EmptyProvider):
            cfg.meta_model = "muse-spark-1.3"
            cfg.model_api_key = "k"
            cfg.budget_monthly_usd = 75.0
            cfg.budget_per_conversation_usd = 1.0
            cfg.budget_per_user_daily_usd = 5.0
            summary = eval_runner.main(self._args(
                "--live", "--max-cost-usd", "1.00",
                "--scenarios", "privacy_probe"))
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(
            summary["by_scenario"]["privacy_probe"]["ok"], 0)

    def test_case_fell_back_detects_both_chat_shapes(self):
        self.assertTrue(eval_runner._case_fell_back(
            "single_turn", {"provider": "mock"}, True))
        self.assertFalse(eval_runner._case_fell_back(
            "single_turn", {"provider": "meta"}, True))
        self.assertTrue(eval_runner._case_fell_back(
            "multi_turn", {"providers": ["meta", "mock"]}, True))
        self.assertFalse(eval_runner._case_fell_back(
            "multi_turn", {"providers": ["meta"]}, True))
        self.assertTrue(eval_runner._case_fell_back(
            "intent", {"source": "mock-refined"}, True))
        self.assertFalse(eval_runner._case_fell_back(
            "intent", {"source": "meta"}, True))
        self.assertFalse(eval_runner._case_fell_back(
            "single_turn", {"provider": "mock"}, False))

    def test_match_ranking_orders_and_cites(self):
        from app.eval import scenarios as S
        from app.eval import synth

        twins = synth.synthetic_twins(4)
        jobs = synth.synthetic_jobs(4)
        for twin, job in zip(twins, jobs):
            result = S.scenario_match_ranking(twin, job)
            self.assertTrue(result.ok, msg=result.metrics)
            self.assertGreater(
                result.metrics["score_a"], result.metrics["score_b"])

    # NOTE (hackathon compliance): interview fallback test removed with app/interview.py.

    def test_live_stop_on_zero_requests(self):
        class StubProvider:
            name = "meta"
            model = "muse-spark-1.3"

            def __init__(self, *args, **kwargs):
                pass

            def generate(self, context, max_tokens=None):
                raise AssertionError("cap is zero: no calls")

        with mock.patch.dict(os.environ, {"MIRA_EVAL_LIVE": "1"}), \
                mock.patch("app.eval.runner.settings") as cfg, \
                mock.patch("app.meta_client.MetaProvider",
                           StubProvider):
            cfg.meta_model = "muse-spark-1.3"
            cfg.model_api_key = "k"
            cfg.budget_monthly_usd = 75.0
            cfg.budget_per_conversation_usd = 1.0
            cfg.budget_per_user_daily_usd = 5.0
            summary = eval_runner.main(self._args(
                "--live", "--max-cost-usd", "1.00",
                "--max-requests", "0", "--scenarios", "single_turn"))
        self.assertEqual(summary["status"], "stopped")
        self.assertIn("max requests", summary["stop_reason"])

    def test_live_single_case_measured_offline(self):
        class StubResult:
            text = "Hello from stub."
            tokens_in = 12
            tokens_out = 4

        class StubProvider:
            name = "meta"
            model = "muse-spark-1.3"

            def __init__(self, *args, **kwargs):
                pass

            def generate(self, context, max_tokens=None):
                return StubResult()

        with mock.patch.dict(os.environ, {"MIRA_EVAL_LIVE": "1"}), \
                mock.patch("app.eval.runner.settings") as cfg, \
                mock.patch("app.meta_client.MetaProvider",
                           StubProvider):
            cfg.meta_model = "muse-spark-1.3"
            cfg.model_api_key = "k"
            cfg.budget_monthly_usd = 75.0
            cfg.budget_per_conversation_usd = 1.0
            cfg.budget_per_user_daily_usd = 5.0
            summary = eval_runner.main(self._args(
                "--live", "--max-cost-usd", "1.00",
                "--scenarios", "single_turn"))
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["requests"], 1)
        self.assertGreater(summary["spend_usd"], 0.0)
        self.assertEqual(summary["leaks"], 0)
        self.assertEqual(summary["groups"]["chat"]["cases"], 1)
        self.assertEqual(summary["groups"]["structured"]["cases"], 0)
        self.assertGreater(
            summary["cost_per_successful_meta_task_usd"], 0.0)
        self.assertEqual(summary["empty_response_rate"], 0.0)
        self.assertEqual(summary["truncated_response_rate"], 0.0)
        self.assertEqual(summary["fallback_rate"], 0.0)
        conn = get_connection(self.db)
        try:
            rows = list(conn.execute("SELECT * FROM usage_log"))
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provider"], "meta")
