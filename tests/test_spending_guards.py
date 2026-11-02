"""Spending-guard regression tests (Step 5). All offline: injected fake
HTTP clients, temporary SQLite databases. No test here can reach a
live provider: every MetaProvider/MetaAdapter is constructed with an
explicit fake `post`, and //guarded// paths are asserted, never live.
"""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from app import budget as budget_engine
from app import conversations as engine
from app.ai.adapters.meta import MetaAdapter
from app.ai.registry import REGISTRY, ProviderSpec
from app.ai.router import run_task
from app.ai.types import TaskContext
from app.conversations import create_conversation
from app.db import (
    create_twin,
    create_user,
    get_connection,
    init_db,
)
from app.intent import interpret_need
from app.meta_client import MetaAPIError, MetaProvider


def _ok_chat(url, key, payload):
    return {"choices": [{"message": {"content": "paid reply"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


class SpendCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "spend.db")
        init_db(self.db)
        self.conn = get_connection(self.db)
        self.addCleanup(self.conn.close)
        self.alice = create_user(self.conn, name="A", role="professional",
                                 email="a@spend.test", password_hash=None)
        self.bob = create_user(self.conn, name="B", role="professional",
                               email="b@spend.test", password_hash=None)
        self.twin_a = create_twin(
            self.conn, user_id=self.alice, display_name="ATwin",
            persona="p", tone="warm", profile={"skills": ["Python"]},
            authorized_fields=["skills"], avatar_seed="a")
        self.twin_b = create_twin(
            self.conn, user_id=self.bob, display_name="BTwin",
            persona="p", tone="warm", profile={}, authorized_fields=[],
            avatar_seed="b")
        self.cid = create_conversation(
            self.conn, twin_a_id=self.twin_a, twin_b_id=self.twin_b,
            created_by_user_id=self.alice)
        self.conn.commit()

    def _rows(self):
        return list(self.conn.execute(
            "SELECT provider, model, status, tokens_in, tokens_out,"
            " est_cost_usd, notes FROM usage_log ORDER BY id"))

    def _guard_probe(self):
        """Record guard/check event order around one paid call."""
        import contextlib

        events = []
        real_guard = budget_engine.paid_call_guard
        orig_check = budget_engine.check_budgets

        @contextlib.contextmanager
        def probe():
            events.append("enter")
            with real_guard():
                yield
            events.append("exit")

        def spy(conn, user_id, conv_id):
            events.append("check")
            return orig_check(conn, user_id, conv_id)

        return probe, spy, events


class TestChatFailureAccounting(SpendCase):
    def test_timeout_leaves_failed_unknown_row(self):
        def timeout(url, key, payload):
            raise TimeoutError("simulated socket timeout")

        provider = MetaProvider(model="muse-spark-1.3", api_key="k",
                                post=timeout)
        result = engine.step_conversation(self.conn, self.cid, self.alice,
                                          provider=provider)
        self.assertEqual(result["message"]["provider"], "mock")
        rows = self._rows()
        self.assertEqual([r["status"] for r in rows],
                         ["failed_unknown", "ok"])
        self.assertEqual(rows[0]["provider"], "meta")
        # Unknown usage is never $0: tokens stay unknown (0, 0) while the
        # conservative estimate is what the budget checks count.
        self.assertEqual((rows[0]["tokens_in"], rows[0]["tokens_out"]),
                         (0, 0))
        self.assertAlmostEqual(
            rows[0]["est_cost_usd"],
            budget_engine.estimate_cost_for(
                "meta", "muse-spark-1.3",
                budget_engine.UNKNOWN_TOKENS_IN,
                budget_engine.UNKNOWN_TOKENS_OUT))
        self.assertGreater(rows[0]["est_cost_usd"], 0.0)
        self.assertIn("timeout", rows[0]["notes"])
        bodies = [m["body"] for m in engine.list_messages(self.conn,
                                                          self.cid)]
        self.assertTrue(any("Meta inference unavailable" in b
                            for b in bodies))
        actions = [r["action"] for r in self.conn.execute(
            "SELECT action FROM audit_events WHERE conv_id = ?",
            (self.cid,))]
        self.assertIn("budget-fallback", actions)

    def test_bad_shape_with_usage_leaves_failed_known(self):
        def bad_shape(url, key, payload):
            return {"choices": [{"message": {}}],
                    "usage": {"prompt_tokens": 100,
                             "completion_tokens": 50}}

        provider = MetaProvider(model="muse-spark-1.3", api_key="k",
                                post=bad_shape)
        result = engine.step_conversation(self.conn, self.cid, self.alice,
                                          provider=provider)
        self.assertEqual(result["message"]["provider"], "mock")
        rows = self._rows()
        self.assertEqual(rows[0]["status"], "failed_known")
        self.assertEqual((rows[0]["tokens_in"], rows[0]["tokens_out"]),
                         (100, 50))
        self.assertGreater(rows[0]["est_cost_usd"], 0.0)
        self.assertGreater(
            budget_engine.conversation_spend(self.conn, self.cid), 0.0)

    def test_repeated_unknown_failures_trip_all_budget_scopes(self):
        # ~24 unknowns at $0.0420 trip the $1 conversation cap, but one
        # conversation caps at 30 turns, so pre-record 23 unknowns via
        # the production ledger, then drive the trip + block through
        # the real chat path: exactly one more HTTP attempt is allowed.
        for _ in range(23):
            budget_engine.record_usage(
                self.conn, provider="meta", model="muse-spark-1.3",
                conv_id=self.cid, user_id=self.alice,
                tokens_in=0, tokens_out=0,
                status="failed_unknown", notes="prior timeout")
        calls = []

        def flaky_timeout(url, key, payload):
            calls.append(url)
            raise TimeoutError("simulated socket timeout")

        provider = MetaProvider(model="muse-spark-1.3", api_key="k",
                                post=flaky_timeout)
        for uid in (self.alice, self.bob, self.alice):
            result = engine.step_conversation(self.conn, self.cid, uid,
                                              provider=provider)
            self.assertEqual(result["message"]["provider"], "mock")
        # The 24th unknown crosses $1; later steps must fall back
        # WITHOUT another HTTP attempt.
        self.assertEqual(len(calls), 1)
        unknowns = [r for r in self._rows()
                    if r["status"] == "failed_unknown"]
        self.assertEqual(len(unknowns), 24)
        self.assertGreaterEqual(
            budget_engine.conversation_spend(self.conn, self.cid), 1.0)
        self.assertGreater(
            budget_engine.monthly_spend(self.conn), 0.0)
        self.assertGreater(
            budget_engine.user_daily_spend(self.conn, self.alice), 0.0)
        with self.assertRaises(budget_engine.BudgetExhausted):
            budget_engine.check_budgets(self.conn, self.alice, self.cid)

    def test_null_usage_is_failed_unknown_with_fallback(self):
        for bad_usage in (None, [1, 2], "10/5", 42):
            with self.subTest(usage=bad_usage):
                def shapeless(url, key, payload, _u=bad_usage):
                    return {"choices": [{"message": {
                        "content": "looks fine"}}], "usage": _u}

                provider = MetaProvider(
                    model="muse-spark-1.3", api_key="k", post=shapeless)
                before = len(self._rows())
                result = engine.step_conversation(
                    self.conn, self.cid, self.alice, provider=provider)
                self.assertEqual(result["message"]["provider"], "mock")
                rows = self._rows()[before:]
                self.assertEqual(rows[0]["status"], "failed_unknown")
                self.assertGreater(rows[0]["est_cost_usd"], 0.0)

    def test_exhausted_budget_leaves_no_meta_row(self):
        budget_engine.record_usage(
            self.conn, provider="meta", model="muse-spark-1.3",
            conv_id=self.cid, user_id=self.alice, tokens_in=0,
            tokens_out=1_000_000)  # way over the $1 conversation cap
        calls = []

        def counting(url, key, payload):
            calls.append(url)
            return _ok_chat(url, key, payload)

        provider = MetaProvider(model="muse-spark-1.3", api_key="k",
                                post=counting)
        result = engine.step_conversation(self.conn, self.cid, self.alice,
                                          provider=provider)
        self.assertEqual(result["message"]["provider"], "mock")
        self.assertEqual(calls, [])
        meta_rows = [r for r in self._rows() if r["provider"] == "meta"]
        self.assertEqual(len(meta_rows), 1)  # only the setup row


class TestNonNegativeUsage(unittest.TestCase):
    def test_reported_usage_rejects_negatives_and_bools(self):
        rep = MetaProvider._reported_usage
        self.assertEqual(
            rep({"usage": {"prompt_tokens": -5,
                           "completion_tokens": 10}}), (None, None))
        self.assertEqual(
            rep({"usage": {"prompt_tokens": 10,
                           "completion_tokens": -1}}), (None, None))
        self.assertEqual(
            rep({"usage": {"prompt_tokens": True,
                           "completion_tokens": 5}}), (None, None))
        self.assertEqual(
            rep({"usage": {"prompt_tokens": 25,
                           "completion_tokens": 7}}), (25, 7))
        self.assertEqual(
            rep({"usage": {"prompt_tokens": "25",
                           "completion_tokens": 7}}), (25, 7))
        self.assertEqual(rep({}), (None, None))
        # Non-dict usage shapes must degrade to unknown, never crash.
        self.assertEqual(rep({"usage": None}), (None, None))
        self.assertEqual(rep({"usage": [10, 5]}), (None, None))
        self.assertEqual(rep({"usage": "10/5"}), (None, None))
        self.assertEqual(rep({"usage": 42}), (None, None))
        self.assertEqual(rep(None), (None, None))

    def test_tool_loop_ignores_negative_usage(self):
        def completed(url, payload, headers):
            return {"object": "response", "status": "completed",
                    "id": "resp-1",
                    "usage": {"input_tokens": -100,
                             "output_tokens": 50},
                    "output": [{"type": "message",
                                "content": [{"type": "output_text",
                                             "text": "hi"}]}]}

        adapter = MetaAdapter(model="muse-spark-1.3", api_key="k",
                              post=completed)
        result = adapter.run_tools(
            [{"role": "user", "content": "hi"}], tools=[], handler=None)
        self.assertEqual(result.tokens_in, 0)
        self.assertEqual(result.tokens_out, 50)


class TestPaidCallGuard(SpendCase):
    def test_paid_calls_serialized(self):
        barrier = threading.Barrier(2)
        errors = []

        class Blocking:
            name = "meta"
            model = "muse-spark-1.3"

            def generate(self, context):
                barrier.wait(timeout=3)
                return mock.Mock(tokens_in=1, tokens_out=1)

        def worker(db_path, out):
            conn = get_connection(db_path)
            try:
                budget_engine.guarded_generate(
                    conn, Blocking(), object(), conv_id=self.cid,
                    user_id=self.alice)
            except Exception as exc:  # noqa: BLE001 - asserting below
                out.append(type(exc).__name__)
            finally:
                conn.close()

        outs = ([], [])
        threads = [threading.Thread(target=worker, args=(self.db, outs[0])),
                   threading.Thread(target=worker, args=(self.db, outs[1]))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        # Both threads must see a broken barrier: the two generate() calls
        # never overlapped, proving mutual exclusion across check->record.
        self.assertEqual(list(outs), [["BrokenBarrierError"],
                                      ["BrokenBarrierError"]])
        self.assertEqual(self._rows(), [])  # nothing recorded on failure

    def test_run_task_checks_inside_guard(self):
        def ok_adapter(url, payload, headers):
            return {"choices": [{"message": {"content": "done"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        saved = REGISTRY._specs["meta"]
        REGISTRY._specs["meta"] = ProviderSpec(
            "meta", lambda: MetaAdapter(model="muse-spark-1.3",
                                        api_key="k", post=ok_adapter),
            "test fake")
        self.addCleanup(lambda: REGISTRY._specs.__setitem__("meta", saved))
        probe, spy, events = self._guard_probe()
        task = TaskContext(task_id="t1", kind="writing",
                           instructions="write")
        with mock.patch.object(budget_engine, "paid_call_guard", probe), \
                mock.patch.object(budget_engine, "check_budgets", spy):
            result = run_task(
                self.conn, task,
                messages=[{"role": "user", "content": "hi"}],
                user_id=self.alice, provider_name="meta")
        self.assertEqual(events, ["enter", "check", "exit"])
        self.assertEqual(result.provider, "meta")
        self.assertEqual(self._rows()[0]["status"], "ok")

    def test_interpret_need_checks_inside_guard(self):
        def ok_json(url, key, payload):
            return {"choices": [{"message": {"content":
                        '{"category": "job", "requirements":'
                        ' {"skills": ["React"]},'
                        ' "confidence": "high"}'}}],
                    "usage": {"prompt_tokens": 40,
                             "completion_tokens": 20}}

        provider = MetaProvider(model="muse-spark-1.3", api_key="k",
                                post=ok_json)
        probe, spy, events = self._guard_probe()
        with mock.patch.object(budget_engine, "paid_call_guard", probe), \
                mock.patch.object(budget_engine, "check_budgets", spy):
            out = interpret_need(
                "Find a React developer available for a project.",
                conn=self.conn, user_id=self.alice, provider=provider)
        self.assertEqual(events, ["enter", "check", "exit"])
        self.assertEqual(out["source"], "meta")


class TestRetryAccounting(SpendCase):
    def _patch_meta(self, post):
        saved = REGISTRY._specs["meta"]
        REGISTRY._specs["meta"] = ProviderSpec(
            "meta", lambda: MetaAdapter(model="muse-spark-1.3",
                                        api_key="k", post=post),
            "test fake")
        self.addCleanup(
            lambda: REGISTRY._specs.__setitem__("meta", saved))

    def _run(self, **kwargs):
        task = TaskContext(task_id="t1", kind="writing",
                           instructions="write")
        kwargs.setdefault("user_id", self.alice)
        kwargs.setdefault("conv_id", self.cid)
        kwargs.setdefault("provider_name", "meta")
        with mock.patch("time.sleep"):
            return run_task(
                self.conn, task,
                messages=[{"role": "user", "content": "hi"}],
                **kwargs)

    def _ok_post(self, tokens_in=10, tokens_out=5):
        def ok(url, payload, headers):
            return {"choices": [{"message": {"content": "done"}}],
                    "usage": {"prompt_tokens": tokens_in,
                              "completion_tokens": tokens_out}}
        return ok

    def test_exhausted_budget_makes_no_attempts(self):
        budget_engine.record_usage(
            self.conn, provider="meta", model="muse-spark-1.3",
            conv_id=self.cid, user_id=self.alice, tokens_in=0,
            tokens_out=1_000_000)  # over the $1 conversation cap
        calls = []

        def counting(url, payload, headers):
            calls.append(url)
            return self._ok_post()(url, payload, headers)

        self._patch_meta(counting)
        with self.assertRaises(budget_engine.BudgetExhausted):
            self._run()
        self.assertEqual(calls, [])
        meta_rows = [r for r in self._rows() if r["provider"] == "meta"]
        self.assertEqual(len(meta_rows), 1)  # only the setup row

    def test_timeout_then_success_records_unknown_plus_ok(self):
        from app.ai.types import ProviderError

        script = [ProviderError("simulated timeout", retryable=True),
                  None]

        def flaky(url, payload, headers):
            nxt = script.pop(0)
            if nxt is not None:
                raise nxt
            return self._ok_post(10, 5)(url, payload, headers)

        self._patch_meta(flaky)
        result = self._run()
        self.assertEqual(result.provider, "meta")
        self.assertEqual(result.attempts, 2)
        rows = self._rows()
        self.assertEqual([r["status"] for r in rows],
                         ["failed_unknown", "ok"])
        self.assertGreater(rows[0]["est_cost_usd"], 0.0)
        self.assertEqual((rows[1]["tokens_in"], rows[1]["tokens_out"]),
                         (10, 5))

    def test_mixed_failures_then_success_separates_known_unknown(self):
        from app.ai.types import ProviderError

        script = [ProviderError("bad shape, but billed", retryable=True,
                                tokens_in=100, tokens_out=50),
                  ProviderError("simulated timeout", retryable=True),
                  None]

        def flaky(url, payload, headers):
            nxt = script.pop(0)
            if nxt is not None:
                raise nxt
            return self._ok_post(10, 5)(url, payload, headers)

        self._patch_meta(flaky)
        result = self._run()
        self.assertEqual(result.attempts, 3)
        rows = self._rows()
        self.assertEqual([r["status"] for r in rows],
                         ["failed_known", "failed_unknown", "ok"])
        # Known usage keeps its exact real cost -- no estimate, no double
        # count when the later attempt succeeds.
        self.assertEqual((rows[0]["tokens_in"], rows[0]["tokens_out"]),
                         (100, 50))
        self.assertAlmostEqual(
            rows[0]["est_cost_usd"],
            budget_engine.estimate_cost_for("meta", "muse-spark-1.3",
                                            100, 50))
        self.assertGreater(rows[1]["est_cost_usd"], 0.0)
        self.assertEqual((rows[2]["tokens_in"], rows[2]["tokens_out"]),
                         (10, 5))

    def test_final_failure_records_every_attempt(self):
        from app.ai.types import ProviderError

        calls = []

        def down(url, payload, headers):
            calls.append(url)
            raise ProviderError("simulated outage", retryable=True)

        self._patch_meta(down)
        result = self._run(max_attempts=3)
        self.assertTrue(result.mock)
        self.assertEqual(len(calls), 3)
        rows = self._rows()
        self.assertEqual(
            [r["status"] for r in rows],
            ["failed_unknown", "failed_unknown", "failed_unknown",
             "ok"])
        self.assertEqual(rows[-1]["provider"], "mock")

    def test_budget_exhaustion_stops_retries(self):
        from app.ai.types import ProviderError

        for _ in range(23):  # $0.9660: one more unknown trips the cap
            budget_engine.record_usage(
                self.conn, provider="meta", model="muse-spark-1.3",
                conv_id=self.cid, user_id=self.alice,
                tokens_in=0, tokens_out=0,
                status="failed_unknown", notes="prior timeout")
        calls = []

        def down(url, payload, headers):
            calls.append(url)
            raise ProviderError("simulated outage", retryable=True)

        self._patch_meta(down)
        with self.assertRaises(budget_engine.BudgetExhausted):
            self._run(max_attempts=3)
        # Attempt 1 runs and is recorded; attempt 2 is refused before
        # any second request.
        self.assertEqual(len(calls), 1)
        unknowns = [r for r in self._rows()
                    if r["status"] == "failed_unknown"]
        self.assertEqual(len(unknowns), 24)


class TestToolLoopPartialUsage(SpendCase):
    def _patch_meta(self, post):
        saved = REGISTRY._specs["meta"]
        REGISTRY._specs["meta"] = ProviderSpec(
            "meta", lambda: MetaAdapter(model="muse-spark-1.3",
                                        api_key="k", post=post),
            "test fake")
        self.addCleanup(
            lambda: REGISTRY._specs.__setitem__("meta", saved))

    def _run_tools(self, **kwargs):
        from app.ai.types import TaskContext

        task = TaskContext(task_id="t1", kind="research",
                           instructions="add")
        kwargs.setdefault("user_id", self.alice)
        kwargs.setdefault("conv_id", self.cid)
        kwargs.setdefault("provider_name", "meta")
        kwargs.setdefault("max_attempts", 1)
        return run_task(
            self.conn, task,
            messages=[{"role": "user", "content": "add 2+3"}],
            tools=[{"name": "add"}],
            tool_handler=lambda n, a: {"sum": 5},
            **kwargs)

    def _round(self, response_id, usage, output):
        return {"object": "response", "id": response_id,
                "status": "completed", "usage": usage,
                "output": output}

    def _call_round(self, response_id, tokens_in, tokens_out):
        return self._round(
            response_id,
            {"input_tokens": tokens_in, "output_tokens": tokens_out},
            [{"type": "function_call", "call_id": "c1",
              "name": "add", "arguments": '{"x": 2, "y": 3}'}])

    def test_ok_round_then_timeout_keeps_partial_plus_unknown(self):
        from app.ai.types import ProviderError

        script = [self._call_round("r1", 10, 5),
                  ProviderError("simulated timeout", retryable=True)]

        def scripted(url, payload, headers):
            nxt = script.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

        self._patch_meta(scripted)
        result = self._run_tools()
        self.assertTrue(result.mock)
        rows = self._rows()
        self.assertEqual([r["status"] for r in rows],
                         ["failed_known", "failed_unknown", "ok"])
        # Known partial round keeps its exact real cost...
        self.assertEqual((rows[0]["tokens_in"], rows[0]["tokens_out"]),
                         (10, 5))
        self.assertAlmostEqual(
            rows[0]["est_cost_usd"],
            budget_engine.estimate_cost_for("meta", "muse-spark-1.3",
                                            10, 5))
        # ...and the uncertain round gets the conservative estimate.
        self.assertEqual((rows[1]["tokens_in"], rows[1]["tokens_out"]),
                         (0, 0))
        self.assertGreater(rows[1]["est_cost_usd"], 0.0)
        self.assertEqual(rows[2]["provider"], "mock")

    def test_ok_round_then_malformed_shape_keeps_partial_plus_unknown(self):
        script = [self._call_round("r1", 10, 5),
                  {"not": "a response"}]

        def scripted(url, payload, headers):
            return script.pop(0)

        self._patch_meta(scripted)
        result = self._run_tools()
        self.assertTrue(result.mock)
        rows = self._rows()
        self.assertEqual([r["status"] for r in rows],
                         ["failed_known", "failed_unknown", "ok"])
        self.assertEqual((rows[0]["tokens_in"], rows[0]["tokens_out"]),
                         (10, 5))
        self.assertGreater(rows[1]["est_cost_usd"], 0.0)

    def test_first_round_timeout_records_zero_known_plus_unknown(self):
        from app.ai.types import ProviderError

        def down(url, payload, headers):
            raise ProviderError("simulated timeout", retryable=True)

        self._patch_meta(down)
        result = self._run_tools()
        self.assertTrue(result.mock)
        rows = self._rows()
        self.assertEqual([r["status"] for r in rows],
                         ["failed_known", "failed_unknown", "ok"])
        # Nothing parsed yet: $0 known row plus the guarding estimate.
        self.assertEqual(rows[0]["est_cost_usd"], 0.0)
        self.assertGreater(rows[1]["est_cost_usd"], 0.0)


class TestRouterRequestLimits(SpendCase):
    def _patch_meta(self, post):
        saved = REGISTRY._specs["meta"]
        REGISTRY._specs["meta"] = ProviderSpec(
            "meta", lambda: MetaAdapter(model="muse-spark-1.3",
                                        api_key="k", post=post),
            "test fake")
        self.addCleanup(
            lambda: REGISTRY._specs.__setitem__("meta", saved))

    def _run(self, messages, **kwargs):
        task = TaskContext(task_id="t1", kind="writing",
                           instructions="write")
        kwargs.setdefault("user_id", self.alice)
        kwargs.setdefault("conv_id", self.cid)
        kwargs.setdefault("provider_name", "meta")
        return run_task(self.conn, task, messages=messages, **kwargs)

    def _ok_post(self):
        def ok(url, payload, headers):
            return {"choices": [{"message": {"content": "done"}}],
                    "usage": {"prompt_tokens": 10,
                              "completion_tokens": 5}}
        return ok

    def test_message_count_boundary(self):
        from app.ai.router import MAX_ROUTER_MESSAGES

        calls = []

        def counting(url, payload, headers):
            calls.append(url)
            return self._ok_post()(url, payload, headers)

        self._patch_meta(counting)
        ok_msgs = [{"role": "user", "content": "hi"}
                   for _ in range(MAX_ROUTER_MESSAGES)]
        result = self._run(ok_msgs)
        self.assertEqual(result.provider, "meta")
        self.assertEqual(len(calls), 1)
        with self.assertRaises(ValueError):
            self._run(ok_msgs + [{"role": "user", "content": "one more"}])
        self.assertEqual(len(calls), 1)  # rejected before any request

    def test_message_size_boundaries(self):
        from app.ai.router import (
            MAX_ROUTER_MESSAGE_CHARS,
            MAX_ROUTER_TOTAL_CHARS,
        )

        self._patch_meta(self._ok_post())
        edge = "x" * MAX_ROUTER_MESSAGE_CHARS
        result = self._run([{"role": "user", "content": edge}])
        self.assertEqual(result.provider, "meta")
        with self.assertRaises(ValueError):
            self._run([{"role": "user", "content": edge + "x"}])
        across = MAX_ROUTER_TOTAL_CHARS // MAX_ROUTER_MESSAGE_CHARS
        total_edge = [{"role": "user", "content": edge}
                      for _ in range(across)]
        result = self._run(total_edge)
        self.assertEqual(result.provider, "meta")
        total_edge.append({"role": "user", "content": "x"})
        with self.assertRaises(ValueError):
            self._run(total_edge)

    def test_rejection_leaves_no_usage_row(self):
        from app.ai.router import MAX_ROUTER_MESSAGES

        calls = []

        def counting(url, payload, headers):
            calls.append(url)
            return self._ok_post()(url, payload, headers)

        self._patch_meta(counting)
        big = [{"role": "user", "content": "hi"}
               for _ in range(MAX_ROUTER_MESSAGES + 1)]
        with self.assertRaises(ValueError):
            self._run(big)
        self.assertEqual(calls, [])
        self.assertEqual(self._rows(), [])

    def test_tool_definition_boundaries(self):
        from app.ai.router import (
            MAX_ROUTER_TOOL_DEFINITIONS_CHARS,
            MAX_ROUTER_TOOLS,
        )

        seen = []

        def immediate(url, payload, headers):
            seen.append(payload)
            return {"object": "response", "id": "r1",
                    "status": "completed",
                    "usage": {"input_tokens": 10,
                              "output_tokens": 5},
                    "output": [{"type": "message", "content": [
                        {"type": "output_text", "text": "done"}]}]}

        self._patch_meta(immediate)
        tools = [{"name": f"t{i}"} for i in range(MAX_ROUTER_TOOLS)]
        task = TaskContext(task_id="t1", kind="research",
                           instructions="go")
        result = run_task(
            self.conn, task, messages=[{"role": "user", "content": "hi"}],
            tools=tools, tool_handler=lambda n, a: {},
            user_id=self.alice, conv_id=self.cid,
            provider_name="meta")
        self.assertEqual(result.provider, "meta")
        with self.assertRaises(ValueError):
            run_task(
                self.conn, task,
                messages=[{"role": "user", "content": "hi"}],
                tools=tools + [{"name": "one-more"}],
                tool_handler=lambda n, a: {},
                user_id=self.alice, conv_id=self.cid,
                provider_name="meta")
        fat = [{"name": "big",
                "description": "y" * MAX_ROUTER_TOOL_DEFINITIONS_CHARS}]
        with self.assertRaises(ValueError):
            run_task(
                self.conn, task,
                messages=[{"role": "user", "content": "hi"}],
                tools=fat, tool_handler=lambda n, a: {},
                user_id=self.alice, conv_id=self.cid,
                provider_name="meta")

    def test_schema_size_boundary(self):
        from app.ai.router import MAX_ROUTER_SCHEMA_CHARS

        self._patch_meta(self._ok_post())
        with self.assertRaises(ValueError):
            self._run([{"role": "user", "content": "hi"}],
                      structured_schema={"blob": "z" * MAX_ROUTER_SCHEMA_CHARS})

    def test_every_request_carries_output_cap(self):
        from app.ai.adapters.meta import MAX_RESPONSES_OUTPUT_TOKENS
        from app.meta_client import MAX_JSON_OUTPUT_TOKENS, MAX_OUTPUT_TOKENS

        seen = []

        def capture(url, payload, headers):
            seen.append((url, payload))
            if url.endswith("/v1/responses"):
                return {"object": "response", "id": "r1",
                        "status": "completed",
                        "usage": {"input_tokens": 10,
                                  "output_tokens": 5},
                        "output": [{"type": "message", "content": [
                            {"type": "output_text", "text": "done"}]}]}
            return self._ok_post()(url, payload, headers)

        self._patch_meta(capture)
        self._run([{"role": "user", "content": "hi"}])
        self.assertEqual(seen[0][1]["max_tokens"], MAX_OUTPUT_TOKENS)
        self._run([{"role": "user", "content": "hi"}],
                  structured_schema={"type": "object"})
        self.assertEqual(seen[1][1]["max_tokens"], MAX_JSON_OUTPUT_TOKENS)
        task = TaskContext(task_id="t1", kind="research",
                           instructions="go")
        run_task(
            self.conn, task, messages=[{"role": "user", "content": "hi"}],
            tools=[{"name": "add"}], tool_handler=lambda n, a: {},
            user_id=self.alice, conv_id=self.cid,
            provider_name="meta")
        responses_payload = seen[2][1]
        self.assertTrue(seen[2][0].endswith("/v1/responses"))
        self.assertEqual(responses_payload["max_output_tokens"],
                         MAX_RESPONSES_OUTPUT_TOKENS)

    def test_tool_rounds_capped_at_five(self):
        from app.ai.adapters.meta import MAX_TOOL_ROUNDS

        calls = []

        def always_calls(url, payload, headers):
            calls.append(url)
            return {"object": "response",
                    "id": f"r{len(calls)}", "status": "completed",
                    "usage": {"input_tokens": 10,
                              "output_tokens": 5},
                    "output": [{"type": "function_call", "call_id": "c1",
                                "name": "add",
                                "arguments": '{"x": 1}'}]}

        self._patch_meta(always_calls)
        task = TaskContext(task_id="t1", kind="research",
                           instructions="go")
        result = run_task(
            self.conn, task, messages=[{"role": "user", "content": "hi"}],
            tools=[{"name": "add"}],
            tool_handler=lambda n, a: {"ok": True},
            user_id=self.alice, conv_id=self.cid,
            provider_name="meta", max_attempts=1)
        self.assertTrue(result.mock)
        self.assertEqual(len(calls), MAX_TOOL_ROUNDS)
        self.assertEqual(MAX_TOOL_ROUNDS, 5)
        rows = self._rows()
        # All five rounds reported usage: one exact known row, no
        # uncertain extra, then the mock fallback row.
        self.assertEqual([r["status"] for r in rows],
                         ["failed_known", "ok"])
        self.assertEqual((rows[0]["tokens_in"], rows[0]["tokens_out"]),
                         (50, 25))


class TestSeederProviderGuard(unittest.TestCase):
    """seed_demo_conversation must never bill a live provider."""

    def setUp(self):
        from app.seed import seed_demo_data, seed_demo_opportunities

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "seed.db")
        init_db(self.db)
        seed_demo_data(self.db)
        seed_demo_opportunities(self.db)
        self.conn = get_connection(self.db)
        self.addCleanup(self.conn.close)

    def _counts(self):
        convs = self.conn.execute(
            "SELECT COUNT(*) AS n FROM conversations").fetchone()["n"]
        usage = self.conn.execute(
            "SELECT COUNT(*) AS n FROM usage_log").fetchone()["n"]
        return convs, usage

    def test_mock_seeding_supported_and_idempotent(self):
        from app.seed import seed_demo_conversation

        first = seed_demo_conversation(self.db)
        self.assertEqual(first, {"created": 1, "skipped": 0})
        providers = {r["provider"] for r in self.conn.execute(
            "SELECT DISTINCT provider FROM messages")}
        self.assertEqual(providers, {"mock"})
        usage_providers = {r["provider"] for r in self.conn.execute(
            "SELECT DISTINCT provider FROM usage_log")}
        self.assertEqual(usage_providers, {"mock"})
        second = seed_demo_conversation(self.db)
        self.assertEqual(second, {"created": 0, "skipped": 1})

    def test_explicit_mock_provider_accepted(self):
        from app.providers import MockProvider
        from app.seed import seed_demo_conversation

        out = seed_demo_conversation(self.db, provider=MockProvider())
        self.assertEqual(out, {"created": 1, "skipped": 0})

    def test_ambient_live_provider_refused_before_any_work(self):
        from app.seed import SeedProviderRefused, seed_demo_conversation

        def boom(url, key, payload):
            raise AssertionError("no HTTP during seeding")

        live = MetaProvider(model="muse-spark-1.3", api_key="k",
                            post=boom)
        with mock.patch("app.providers.get_provider",
                        return_value=live):
            with self.assertRaises(SeedProviderRefused):
                seed_demo_conversation(self.db)
        # Refused before any provider request AND before any mutation
        # by this seeding workflow: no chat, no usage rows.
        self.assertEqual(self._counts(), (0, 0))

    def test_explicit_live_provider_refused_without_http(self):
        from app.seed import SeedProviderRefused, seed_demo_conversation

        calls = []

        def counting(url, key, payload):
            calls.append(url)
            return _ok_chat(url, key, payload)

        live = MetaProvider(model="muse-spark-1.3", api_key="k",
                            post=counting)
        with self.assertRaises(SeedProviderRefused):
            seed_demo_conversation(self.db, provider=live)
        self.assertEqual(calls, [])
        self.assertEqual(self._counts(), (0, 0))


if __name__ == "__main__":
    unittest.main()
