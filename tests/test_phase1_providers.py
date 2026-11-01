"""Phase 1 tests: provider registry, routing, retry, telemetry,
adapters (fake transports only -- no live vendor calls), pricing
honesty, task orchestration, and the v8 telemetry migration."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import budget
from app.ai import (
    AnthropicAdapter,
    GeminiAdapter,
    MetaAdapter,
    MockAdapter,
    OpenAIAdapter,
    ProviderError,
    ProviderNotConfigured,
    UnsupportedOperation,
    call_with_retry,
    new_project,
    new_task,
    parse_routes,
    record_ai_usage,
    route,
    run_task,
    timed,
)
from app.ai.registry import REGISTRY
from app.db import (
    SCHEMA_VERSION,
    driver_name,
    get_connection,
    init_db,
)


class AIcase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.db = str(Path(self._td.name) / "ai.db")
        init_db(self.db)
        self.conn = get_connection(self.db)

    def tearDown(self):
        self.conn.close()
        self._td.cleanup()


class TestCapabilities(unittest.TestCase):
    def test_meta_supports_text_structured_tools(self):
        caps = MetaAdapter(model="muse-spark-1.3",
                           api_key="k").capabilities
        self.assertTrue(caps.text)
        self.assertTrue(caps.structured_output)
        self.assertTrue(caps.tool_calls)
        self.assertFalse(caps.images_out)
        self.assertFalse(caps.audio_out)

    def test_support_matrix(self):
        self.assertTrue(
            OpenAIAdapter(model="m", api_key="k").capabilities.tool_calls)
        self.assertTrue(
            AnthropicAdapter(model="m", api_key="k").capabilities.tool_calls)
        gemini = GeminiAdapter(model="m", api_key="k").capabilities
        self.assertTrue(gemini.text and gemini.structured_output)
        self.assertFalse(gemini.tool_calls)
        mock_caps = MockAdapter().capabilities
        self.assertTrue(mock_caps.text)
        self.assertFalse(mock_caps.tool_calls)

    def test_unsupported_operations_refuse(self):
        gemini = GeminiAdapter(model="m", api_key="k")
        with self.assertRaises(UnsupportedOperation):
            gemini.run_tools([], [], lambda n, a: {})
        with self.assertRaises(UnsupportedOperation):
            MockAdapter().run_tools([], [], lambda n, a: {})


class TestRegistry(unittest.TestCase):
    def test_known_vendors_resolve(self):
        self.assertEqual(REGISTRY.get("mock").name, "mock")
        self.assertEqual(REGISTRY.get("META").name, "meta")
        self.assertEqual(REGISTRY.get("openai").name, "openai")
        self.assertEqual(REGISTRY.get("gemini").name, "gemini")
        self.assertEqual(REGISTRY.get("anthropic").name, "anthropic")

    def test_unknown_name_warns_and_returns_mock(self):
        with self.assertLogs("mira", level="WARNING"):
            adapter = REGISTRY.get("chatgpt")
        self.assertEqual(adapter.name, "mock")

    def test_availability_matrix(self):
        self.assertTrue(MockAdapter().available())
        self.assertFalse(MetaAdapter(model="", api_key="").available())
        self.assertTrue(MetaAdapter(model="muse-spark-1.3",
                                    api_key="k").available())
        self.assertFalse(MetaAdapter(model="muse-spark-1.3-contributor",
                                     api_key="k").available())
        self.assertFalse(OpenAIAdapter(model="", api_key="").available())
        self.assertTrue(
            OpenAIAdapter(model="m", api_key="k").available())


class TestRouter(unittest.TestCase):
    def _settings(self, **overrides):
        defaults = {"ai_provider": "mock", "ai_routes": "",
                    "meta_model": "", "model_api_key": "",
                    "openai_model": "", "openai_api_key": "",
                    "gemini_model": "", "gemini_api_key": "",
                    "anthropic_model": "", "anthropic_api_key": ""}
        defaults.update(overrides)
        return defaults

    def _patched(self, **overrides):
        values = self._settings(**overrides)
        return (mock.patch("app.ai.registry.settings", **values),
                mock.patch("app.ai.router.settings", **values))

    def test_parse_routes(self):
        self.assertEqual(parse_routes("code:openai,intent:meta"),
                         {"code": "openai", "intent": "meta"})
        self.assertEqual(parse_routes("bogus,,:x"), {})
        self.assertEqual(parse_routes(""), {})

    def test_default_is_mock(self):
        reg, rou = self._patched(ai_provider="")
        with reg, rou:
            self.assertEqual(route("chat").name, "mock")

    def test_routes_map_selects_configured_vendor(self):
        reg, rou = self._patched(
            ai_routes="code:meta", meta_model="muse-spark-1.3",
            model_api_key="k")
        with reg, rou:
            self.assertEqual(route("code").name, "meta")
            self.assertEqual(route("chat").name, "mock")

    def test_unavailable_vendor_falls_through(self):
        reg, rou = self._patched(ai_routes="code:openai",
                                 openai_model="m",
                                 openai_api_key="")  # missing -> skip
        with reg, rou:
            self.assertEqual(route("code").name, "mock")

    def test_impossible_capability_raises(self):
        reg, rou = self._patched()
        with reg, rou:
            with self.assertRaises(ProviderNotConfigured):
                route("image", capability="images_out",
                      allow_mock=False)


class TestRetry(unittest.TestCase):
    def test_flaky_then_ok(self):
        delays = []
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise ProviderError("busy", retryable=True)
            return "done"

        result, attempts = call_with_retry(
            flaky, attempts=3, sleeper=delays.append)
        self.assertEqual((result, attempts), ("done", 3))
        self.assertEqual(delays, [1.0, 2.0])

    def test_non_retryable_raises_immediately(self):
        delays = []

        def bad():
            raise ProviderError("nope", retryable=False)

        with self.assertRaises(ProviderError):
            call_with_retry(bad, attempts=3, sleeper=delays.append)
        self.assertEqual(delays, [])

    def test_retry_after_wins(self):
        delays = []
        calls = []

        def limited():
            calls.append(1)
            if len(calls) == 1:
                raise ProviderError("slow", retryable=True,
                                    retry_after=7.5)
            return "ok"

        result, attempts = call_with_retry(
            limited, attempts=2, sleeper=delays.append)
        self.assertEqual(result, "ok")
        self.assertEqual(delays, [7.5])

    def test_exhaustion_reraises(self):
        def always():
            raise ProviderError("down", retryable=True)

        with self.assertRaises(ProviderError):
            call_with_retry(always, attempts=2, sleeper=lambda s: None)


class TestTelemetry(AIcase):
    def test_timed_measures(self):
        result, latency = timed(lambda: 42)
        self.assertEqual(result, 42)
        self.assertGreaterEqual(latency, 0)

    def test_record_ai_usage_stores_telemetry(self):
        record_ai_usage(
            self.conn, provider="mock", model="mock", conv_id=1,
            user_id=1, latency_ms=123, attempts=2)
        row = self.conn.execute("SELECT * FROM usage_log").fetchone()
        self.assertEqual((row["latency_ms"], row["attempts"]), (123, 2))
        summary = budget.usage_summary(self.conn, 1)
        self.assertEqual(summary["avg_latency_ms"], 123)


class TestMetaAdapter(unittest.TestCase):
    def test_text_parse_and_usage(self):
        def fake(url, payload, headers):
            self.assertIn("/v1/chat/completions", url)
            self.assertEqual(payload["model"], "muse-spark-1.3")
            return {"choices": [{"message": {"content": "Hi."}}],
                    "usage": {"prompt_tokens": 11,
                              "completion_tokens": 4}}

        out = MetaAdapter(model="muse-spark-1.3", api_key="k",
                          post=fake).generate_text(
            [{"role": "user", "content": "hi"}])
        self.assertEqual(
            (out.text, out.tokens_in, out.tokens_out, out.mock),
            ("Hi.", 11, 4, False))

    def test_structured_parse(self):
        def fake(url, payload, headers):
            self.assertEqual(
                payload["response_format"]["type"], "json_schema")
            return {"choices": [{"message": {"content": '{"a": 1}'}}],
                    "usage": {"prompt_tokens": 5,
                              "completion_tokens": 2}}

        out = MetaAdapter(model="muse-spark-1.3", api_key="k",
                          post=fake).generate_structured(
            [{"role": "user", "content": "hi"}], {"type": "object"})
        self.assertEqual(out.structured, {"a": 1})

    def test_unconfigured_refuses(self):
        from app.ai.types import ProviderNotConfigured

        adapter = MetaAdapter(model="", api_key="")
        with self.assertRaises(ProviderNotConfigured):
            adapter.generate_text([])

    def test_responses_tool_loop(self):
        seen = []

        def fake(url, payload, headers):
            seen.append(payload)
            if len(seen) == 1:
                self.assertIn("/v1/responses", url)
                self.assertEqual(payload["tool_choice"], "auto")
                return {
                    "object": "response", "id": "r1",
                    "status": "completed",
                    "output": [
                        {"type": "function_call", "call_id": "c1",
                         "name": "add",
                         "arguments": '{"x": 2, "y": 3}'}],
                    "usage": {"input_tokens": 10,
                              "output_tokens": 5}}
            return {
                "object": "response", "id": "r2", "status": "completed",
                "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": "Five."}]}],
                "usage": {"input_tokens": 20, "output_tokens": 3}}

        calls = []

        def handler(name, args):
            calls.append((name, args))
            return {"sum": args["x"] + args["y"]}

        out = MetaAdapter(model="muse-spark-1.3", api_key="k",
                          post=fake).run_tools(
            [{"role": "user", "content": "add 2+3"}],
            [{"name": "add"}], handler)
        self.assertEqual(out.text, "Five.")
        self.assertEqual(calls, [("add", {"x": 2, "y": 3})])
        self.assertEqual((out.tokens_in, out.tokens_out), (30, 8))
        self.assertEqual(seen[1].get("previous_response_id"), "r1")


class TestOpenAIAdapter(unittest.TestCase):
    def test_text_and_tools(self):
        def fake_text(url, payload, headers):
            return {"choices": [{"message": {"content": "Yo."}}],
                    "usage": {"prompt_tokens": 3,
                              "completion_tokens": 1}}

        out = OpenAIAdapter(model="m", api_key="k",
                            post=fake_text).generate_text([])
        self.assertEqual((out.text, out.provider), ("Yo.", "openai"))

        def fake_tools(url, payload, headers):
            if len(payload["messages"]) == 1:
                return {"choices": [{"message": {
                    "content": "",
                    "tool_calls": [{"id": "t1", "type": "function",
                                    "function": {
                                        "name": "now",
                                        "arguments": "{}"}}]}}],
                    "usage": {"prompt_tokens": 4,
                              "completion_tokens": 2}}
            return {"choices": [{"message": {"content": "Noon."}}],
                    "usage": {"prompt_tokens": 8,
                              "completion_tokens": 1}}

        out = OpenAIAdapter(model="m", api_key="k",
                            post=fake_tools).run_tools(
            [{"role": "user", "content": "time?"}],
            [{"name": "now"}], lambda n, a: "noon")
        self.assertEqual(out.text, "Noon.")
        self.assertEqual((out.tokens_in, out.tokens_out), (12, 3))

    def test_unconfigured_refuses(self):
        with self.assertRaises(ProviderNotConfigured):
            OpenAIAdapter(model="", api_key="").generate_text([])


class TestAnthropicAdapter(unittest.TestCase):
    def test_text_and_structured(self):
        bodies = []

        def fake(url, payload, headers):
            bodies.append(payload)
            self.assertEqual(headers["anthropic-version"], "2023-06-01")
            return {"content": [{"type": "text", "text": '{"ok": true}'}],
                    "usage": {"input_tokens": 6,
                              "completion_tokens": 2}}

        adapter = AnthropicAdapter(model="m", api_key="k", post=fake)
        out = adapter.generate_structured([], {"type": "object"})
        self.assertEqual(out.structured, {"ok": True})
        self.assertIn("JSON object", bodies[0]["messages"][-1]["content"])

    def test_tool_loop(self):
        def fake(url, payload, headers):
            if len(payload["messages"]) == 1:
                return {"content": [
                    {"type": "tool_use", "id": "u1",
                     "name": "ping", "input": {}}],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 5,
                              "completion_tokens": 2}}
            return {"content": [{"type": "text", "text": "Pong."}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 9,
                              "completion_tokens": 1}}

        out = AnthropicAdapter(model="m", api_key="k",
                               post=fake).run_tools(
            [{"role": "user", "content": "ping"}],
            [{"name": "ping"}], lambda n, a: "pong")
        self.assertEqual(out.text, "Pong.")


class TestGeminiAdapter(unittest.TestCase):
    def test_text_and_structured(self):
        seen = []

        def fake(url, payload, headers):
            seen.append((url, payload))
            return {"candidates": [{"content": {"parts": [
                {"text": '{"a": 1}'}]}}],
                "usageMetadata": {"promptTokenCount": 7,
                                  "candidatesTokenCount": 3}}

        adapter = GeminiAdapter(model="gem", api_key="k", post=fake)
        out = adapter.generate_structured(
            [{"role": "user", "content": "hi"}], {"type": "object"})
        self.assertEqual(
            (out.structured, out.tokens_in, out.tokens_out),
            ({"a": 1}, 7, 3))
        self.assertIn("models/gem:generateContent", seen[0][0])
        self.assertEqual(
            seen[0][1]["generationConfig"]["responseMimeType"],
            "application/json")


class TestVendorPricing(unittest.TestCase):
    def test_mock_is_free_meta_is_priced(self):
        self.assertEqual(budget.estimate_cost_for("mock", "mock", 9, 9),
                         0.0)
        self.assertAlmostEqual(
            budget.estimate_cost_for("meta", "muse-spark-1.3",
                                     1_000_000, 0), 1.25)

    def test_unpriced_vendor_refused(self):
        with self.assertRaises(budget.BudgetExhausted):
            budget.estimate_cost_for("openai", "m", 10, 10)


class TestRunTask(AIcase):
    def test_mock_task_records_usage_and_chains(self):
        project = new_project("p1", "demo goal")
        task = new_task(project, "t1", "chat", "say hi")
        result = run_task(self.conn, task,
                          messages=[{"role": "user",
                                     "content": "hi"}],
                          conv_id=1, user_id=1)
        self.assertTrue(result.mock)
        self.assertEqual(result.provider, "mock")
        row = self.conn.execute("SELECT * FROM usage_log").fetchone()
        self.assertEqual((row["provider"], row["attempts"]),
                         ("mock", 1))
        chained = task.with_output(result)
        self.assertEqual(len(chained.prior_outputs), 1)
        self.assertTrue(project.history)

    def test_unpriced_vendor_never_calls_network(self):
        project = new_project("p1", "demo goal")
        task = new_task(project, "t1", "chat", "hi")
        with mock.patch("app.ai.registry.settings") as reg_cfg, \
                mock.patch("app.ai.router.settings") as rou_cfg:
            for cfg in (reg_cfg, rou_cfg):
                cfg.ai_provider = "openai"
                cfg.ai_routes = ""
                cfg.openai_model = "m"
                cfg.openai_api_key = "k"
                cfg.meta_model = ""
                cfg.model_api_key = ""
                cfg.gemini_model = ""
                cfg.gemini_api_key = ""
                cfg.anthropic_model = ""
                cfg.anthropic_api_key = ""
            with mock.patch.object(
                    OpenAIAdapter, "generate_text",
                    side_effect=AssertionError("no network")):
                with self.assertRaises(budget.BudgetExhausted):
                    run_task(self.conn, task, messages=[],
                             conv_id=1, user_id=1,
                             fallback_to_mock=False)
        left = self.conn.execute(
            "SELECT COUNT(*) AS n FROM usage_log").fetchone()
        self.assertEqual(left["n"], 0)

    def test_failed_call_leaves_failed_row_then_mock(self):
        budget.VENDOR_PRICES[("openai", "m")] = (1.0, 2.0)
        self.addCleanup(budget.VENDOR_PRICES.pop, ("openai", "m"))
        with mock.patch("app.ai.registry.settings") as reg_cfg, \
                mock.patch("app.ai.router.settings") as rou_cfg, \
                mock.patch("app.ai.adapters.base.http_post_json",
                           side_effect=ProviderError("down",
                                                     retryable=False)):
            for cfg in (reg_cfg, rou_cfg):
                cfg.ai_provider = "openai"
                cfg.ai_routes = ""
                cfg.openai_model = "m"
                cfg.openai_api_key = "k"
                cfg.meta_model = ""
                cfg.model_api_key = ""
                cfg.gemini_model = ""
                cfg.gemini_api_key = ""
                cfg.anthropic_model = ""
                cfg.anthropic_api_key = ""
            project = new_project("p1", "g")
            task = new_task(project, "t1", "chat", "hi")
            result = run_task(self.conn, task, messages=[],
                              conv_id=1, user_id=1)
        self.assertTrue(result.mock)
        statuses = [r["status"] for r in self.conn.execute(
            "SELECT status FROM usage_log ORDER BY id")]
        self.assertEqual(statuses, ["failed_unknown", "ok"])


class TestMigrationV8(AIcase):
    def test_telemetry_columns_and_version(self):
        if driver_name(self.conn) == "postgres":
            rows = self.conn.execute(
                "SELECT column_name AS name"
                " FROM information_schema.columns"
                " WHERE table_schema = current_schema()"
                " AND table_name = 'usage_log'")
        else:
            rows = self.conn.execute("PRAGMA table_info(usage_log)")
        cols = {r["name"] for r in rows}
        self.assertTrue({"latency_ms", "attempts"} <= cols)
        version = self.conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()["version"]
        self.assertEqual(version, SCHEMA_VERSION)
        # Pinned deliberately: bump only with a real migration.
        self.assertEqual(SCHEMA_VERSION, 12)


class TestBackCompat(unittest.TestCase):
    def test_legacy_router_preserved(self):
        from app.providers import (
            MockProvider,
            ProviderResult,
            get_provider,
        )

        self.assertIsInstance(get_provider(), MockProvider)
        self.assertIsInstance(get_provider("mock"), MockProvider)
        self.assertEqual(get_provider().name, "mock")
        self.assertEqual(ProviderResult(text="x").tokens_in, 0)

    def test_unknown_legacy_name_warns(self):
        from app.providers import get_provider

        with self.assertLogs("mira", level="WARNING"):
            provider = get_provider("chatgpt")
        self.assertEqual(provider.name, "mock")


if __name__ == "__main__":
    unittest.main()
