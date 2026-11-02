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

    def test_explicit_meta_missing_config_raises_not_mock(self):
        # No model/key configured: an explicit Meta request must refuse,
        # never silently return the mock adapter as a "meta" result.
        reg, rou = self._patched(meta_model="", model_api_key="")
        with reg, rou:
            with self.assertRaises(ProviderNotConfigured) as ctx:
                route("chat", explicit="meta")
            self.assertIn("meta", str(ctx.exception).lower())

    def test_explicit_meta_unpriced_model_raises_not_mock(self):
        # Unknown model id: MetaAdapter is unavailable, so the explicit
        # request must refuse rather than fall through to mock.
        reg, rou = self._patched(meta_model="muse-spark-9.9",
                                 model_api_key="k")
        with reg, rou:
            with self.assertRaises(ProviderNotConfigured):
                route("chat", explicit="META")  # case-insensitive

    def test_explicit_meta_without_capability_raises(self):
        reg, rou = self._patched(meta_model="muse-spark-1.3",
                                 model_api_key="k")
        with reg, rou:
            with self.assertRaises(ProviderNotConfigured):
                route("image", capability="images_out",
                      explicit="meta")

    def test_explicit_mock_still_works(self):
        # Intentional offline/demo use is preserved: explicit mock
        # keeps returning the mock adapter for supported capabilities.
        reg, rou = self._patched()
        with reg, rou:
            self.assertEqual(route("chat", explicit="mock").name,
                             "mock")
            self.assertEqual(
                route("chat", explicit="mock",
                      capability="text").name, "mock")


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

    def test_structured_envelope_matches_legacy_builder(self):
        from app.meta_client import MetaProvider, response_format_block

        seen = {}

        def fake_adapter(url, payload, headers):
            seen["adapter"] = payload["response_format"]
            return {"choices": [{"message": {"content": '{"a": 1}'}}],
                    "usage": {"prompt_tokens": 5,
                              "completion_tokens": 2}}

        def fake_legacy(url, key, payload):
            seen["legacy"] = payload["response_format"]
            return {"choices": [{"message": {"content": '{"a": 1}'}}],
                    "usage": {"prompt_tokens": 5,
                              "completion_tokens": 2}}

        MetaAdapter(model="muse-spark-1.3", api_key="k",
                    post=fake_adapter).generate_structured(
            [{"role": "user", "content": "hi"}], {"type": "object"})
        MetaProvider(model="muse-spark-1.3", api_key="k",
                     post=fake_legacy).generate_json(
            [{"role": "user", "content": "hi"}], {"type": "object"})
        for side in ("adapter", "legacy"):
            self.assertEqual(set(seen[side]), {"type", "json_schema"})
            self.assertEqual(seen[side]["type"], "json_schema")
            self.assertEqual(set(seen[side]["json_schema"]),
                             {"name", "schema"})
        self.assertEqual(
            response_format_block("n", {"type": "object"}),
            {"type": "json_schema",
             "json_schema": {"name": "n",
                             "schema": {"type": "object"}}})

    def test_structured_default_cap_is_4000_in_both_payloads(self):
        # Live evidence (runs 14-15): structured calls starve at 2000
        # (~95% reasoning overhead); 4000 is the measured floor fix.
        from app.meta_client import MAX_JSON_OUTPUT_TOKENS, MetaProvider

        self.assertEqual(MAX_JSON_OUTPUT_TOKENS, 4000)
        seen = {}

        def fake_adapter(url, payload, headers):
            seen["adapter"] = payload["max_tokens"]
            return {"choices": [{"message": {"content": '{"a": 1}'}}],
                    "usage": {"prompt_tokens": 5,
                              "completion_tokens": 2}}

        def fake_legacy(url, key, payload):
            seen["legacy"] = payload["max_tokens"]
            return {"choices": [{"message": {"content": '{"a": 1}'}}],
                    "usage": {"prompt_tokens": 5,
                              "completion_tokens": 2}}

        MetaAdapter(model="muse-spark-1.3", api_key="k",
                    post=fake_adapter).generate_structured(
            [{"role": "user", "content": "hi"}], {"type": "object"})
        MetaProvider(model="muse-spark-1.3", api_key="k",
                     post=fake_legacy).generate_json(
            [{"role": "user", "content": "hi"}], {"type": "object"})
        self.assertEqual(seen, {"adapter": 4000, "legacy": 4000})

    def test_diagnostics_flow_to_result_and_ledger(self):
        def fake(url, payload, headers):
            return {"choices": [{"finish_reason": "stop",
                                 "message": {"content": "OK"}}],
                    "usage": {"prompt_tokens": 10,
                              "completion_tokens": 200,
                              "completion_tokens_details": {
                                  "reasoning_tokens": 180}}}

        adapter = MetaAdapter(model="muse-spark-1.3", api_key="k",
                              post=fake)
        out = adapter.generate_text([{"role": "user", "content": "hi"}])
        self.assertEqual(out.finish_reason, "stop")
        self.assertEqual(out.reasoning_tokens, 180)

    def test_truncation_is_retryable_but_parse_errors_are_not(self):
        from app.ai.types import ProviderError

        def truncated(url, payload, headers):
            return {"choices": [{"finish_reason": "length",
                                 "message": {"content": ""}}],
                    "usage": {"prompt_tokens": 200,
                              "completion_tokens": 1500}}

        def malformed(url, payload, headers):
            return {"choices": [{"message": {"content": "Hi."}}],
                    "usage": {"prompt_tokens": True,
                              "completion_tokens": -3}}

        with self.assertRaises(ProviderError) as ctx:
            MetaAdapter(model="muse-spark-1.3", api_key="k",
                        post=truncated).generate_text([])
        self.assertTrue(ctx.exception.retryable)
        self.assertTrue(ctx.exception.usage_known)
        with self.assertRaises(ProviderError) as ctx:
            MetaAdapter(model="muse-spark-1.3", api_key="k",
                        post=malformed).generate_text([])
        self.assertFalse(ctx.exception.retryable)
        self.assertFalse(ctx.exception.usage_known)

    def test_default_max_tokens_is_measured_floor(self):
        # Live evidence: 300 starved 20/20 calls; 1500 is the floor
        # that lets genuine replies through. The 64-token smoke
        # override still works (see test_max_tokens_override_*).
        seen = []

        def fake(url, payload, headers):
            seen.append(payload)
            return {"choices": [{"message": {"content": "Hi."}}],
                    "usage": {"prompt_tokens": 11,
                              "completion_tokens": 4}}

        MetaAdapter(model="muse-spark-1.3", api_key="k",
                    post=fake).generate_text(
            [{"role": "user", "content": "hi"}])
        self.assertEqual(seen[0]["max_tokens"], 1500)

    def test_max_tokens_override_reaches_payload(self):
        seen = []

        def fake(url, payload, headers):
            seen.append(payload)
            return {"choices": [{"message": {"content": "OK"}}],
                    "usage": {"prompt_tokens": 10,
                              "completion_tokens": 2}}

        out = MetaAdapter(model="muse-spark-1.3", api_key="k",
                          post=fake).generate_text(
            [{"role": "user", "content": "hi"}], max_tokens=64)
        self.assertEqual(seen[0]["max_tokens"], 64)
        self.assertEqual(
            (out.text, out.tokens_in, out.tokens_out), ("OK", 10, 2))

    def test_max_tokens_invalid_rejected_without_network(self):
        calls = []

        def fake(url, payload, headers):
            calls.append(url)
            return {"choices": [{"message": {"content": "Hi."}}],
                    "usage": {"prompt_tokens": 11,
                              "completion_tokens": 4}}

        adapter = MetaAdapter(model="muse-spark-1.3", api_key="k",
                              post=fake)
        for bad in (0, -1, -1500, True, False, 1501, 2000,
                    "64", 64.0, [64], {"max_tokens": 64}):
            with self.subTest(limit=bad):
                with self.assertRaises(ValueError):
                    adapter.generate_text(
                        [{"role": "user", "content": "hi"}],
                        max_tokens=bad)
        self.assertEqual(calls, [])

    def test_max_tokens_boundaries_accepted(self):
        seen = []

        def fake(url, payload, headers):
            seen.append(payload)
            return {"choices": [{"message": {"content": "OK"}}],
                    "usage": {"prompt_tokens": 10,
                              "completion_tokens": 2}}

        adapter = MetaAdapter(model="muse-spark-1.3", api_key="k",
                              post=fake)
        for good in (1, 64, 300, 1500):
            adapter.generate_text([{"role": "user", "content": "hi"}],
                                  max_tokens=good)
        self.assertEqual([p["max_tokens"] for p in seen],
                         [1, 64, 300, 1500])

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
                              "output_tokens": 2}}

        adapter = AnthropicAdapter(model="m", api_key="k", post=fake)
        out = adapter.generate_structured([], {"type": "object"})
        self.assertEqual(out.structured, {"ok": True})
        self.assertEqual((out.tokens_in, out.tokens_out), (6, 2))
        self.assertIn("JSON object", bodies[0]["messages"][-1]["content"])

    def test_tool_loop(self):
        def fake(url, payload, headers):
            if len(payload["messages"]) == 1:
                return {"content": [
                    {"type": "tool_use", "id": "u1",
                     "name": "ping", "input": {}}],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 5,
                              "output_tokens": 2}}
            return {"content": [{"type": "text", "text": "Pong."}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 9,
                              "output_tokens": 1}}

        out = AnthropicAdapter(model="m", api_key="k",
                               post=fake).run_tools(
            [{"role": "user", "content": "ping"}],
            [{"name": "ping"}], lambda n, a: "pong")
        self.assertEqual(out.text, "Pong.")
        self.assertEqual((out.tokens_in, out.tokens_out), (14, 3))


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
                          conv_id=1, user_id=1, provider_name="mock")
        self.assertTrue(result.mock)
        self.assertEqual(result.provider, "mock")
        row = self.conn.execute("SELECT * FROM usage_log").fetchone()
        self.assertEqual((row["provider"], row["attempts"]),
                         ("mock", 1))
        chained = task.with_output(result)
        self.assertEqual(len(chained.prior_outputs), 1)
        self.assertTrue(project.history)

    def test_explicit_meta_unconfigured_leaves_no_mock_row(self):
        # End to end: an explicit Meta request with Meta unconfigured
        # raises before routing, budgeting, or any fallback -- no mock
        # result is ever returned as a successful Meta result.
        project = new_project("p1", "g")
        task = new_task(project, "t1", "chat", "hi")
        with mock.patch("app.ai.registry.settings") as reg_cfg, \
                mock.patch("app.ai.router.settings") as rou_cfg:
            for cfg in (reg_cfg, rou_cfg):
                cfg.ai_provider = "mock"
                cfg.ai_routes = ""
                cfg.meta_model = ""
                cfg.model_api_key = ""
                cfg.openai_model = ""
                cfg.openai_api_key = ""
                cfg.gemini_model = ""
                cfg.gemini_api_key = ""
                cfg.anthropic_model = ""
                cfg.anthropic_api_key = ""
            with self.assertRaises(ProviderNotConfigured):
                run_task(self.conn, task, messages=[],
                         conv_id=1, user_id=1, provider_name="meta")
        left = self.conn.execute(
            "SELECT COUNT(*) AS n FROM usage_log").fetchone()
        self.assertEqual(left["n"], 0)

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

    def _priced_openai(self):
        budget.VENDOR_PRICES[("openai", "m")] = (1.0, 2.0)
        self.addCleanup(budget.VENDOR_PRICES.pop, ("openai", "m"))
        reg = mock.patch("app.ai.registry.settings")
        rou = mock.patch("app.ai.router.settings")
        return reg, rou

    def _configure_openai(self, reg_cfg, rou_cfg):
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

    def test_unknown_vendor_usage_leaves_estimated_row(self):
        # Valid text but no usage block: the ledger must hold a
        # failed_unknown estimate row, never a $0 "ok" row.
        reg, rou = self._priced_openai()
        with reg as reg_cfg, rou as rou_cfg, mock.patch(
                "app.ai.adapters.base.http_post_json",
                return_value={"choices": [{"message": {
                    "content": "Yo."}}]}):
            self._configure_openai(reg_cfg, rou_cfg)
            project = new_project("p1", "g")
            task = new_task(project, "t1", "chat", "hi")
            result = run_task(self.conn, task, messages=[],
                              conv_id=1, user_id=1)
        self.assertTrue(result.mock)
        rows = list(self.conn.execute(
            "SELECT status, tokens_in, tokens_out, est_cost_usd"
            " FROM usage_log ORDER BY id"))
        self.assertEqual([r["status"] for r in rows],
                         ["failed_unknown", "ok"])
        self.assertEqual((rows[0]["tokens_in"], rows[0]["tokens_out"]),
                         (0, 0))
        self.assertGreater(rows[0]["est_cost_usd"], 0.0)

    def test_openai_tool_loop_keeps_partial_plus_unknown(self):
        # First tool round reports usage, the second omits it: the
        # known round keeps its exact cost and the uncertain round
        # gets the conservative estimate.
        script = [
            {"choices": [{"message": {
                "content": "",
                "tool_calls": [{"id": "t1", "type": "function",
                                "function": {"name": "now",
                                             "arguments": "{}"}}]}}],
             "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
            {"choices": [{"message": {"content": "Noon."}}]},
        ]
        reg, rou = self._priced_openai()
        with reg as reg_cfg, rou as rou_cfg, mock.patch(
                "app.ai.adapters.base.http_post_json",
                side_effect=lambda *a: script.pop(0)):
            self._configure_openai(reg_cfg, rou_cfg)
            project = new_project("p1", "g")
            task = new_task(project, "t1", "research", "time?")
            result = run_task(
                self.conn, task, messages=[{"role": "user",
                                            "content": "time?"}],
                tools=[{"name": "now"}],
                tool_handler=lambda n, a: "noon",
                conv_id=1, user_id=1, max_attempts=1)
        self.assertTrue(result.mock)
        rows = list(self.conn.execute(
            "SELECT status, tokens_in, tokens_out, est_cost_usd"
            " FROM usage_log ORDER BY id"))
        self.assertEqual([r["status"] for r in rows],
                         ["failed_known", "failed_unknown", "ok"])
        self.assertEqual((rows[0]["tokens_in"], rows[0]["tokens_out"]),
                         (10, 5))
        self.assertGreater(rows[1]["est_cost_usd"], 0.0)

    def test_run_task_passes_max_tokens_to_meta(self):
        from app.ai.registry import ProviderSpec

        seen = []

        def fake(url, payload, headers):
            seen.append(payload)
            return {"choices": [{"message": {"content": "OK"}}],
                    "usage": {"prompt_tokens": 10,
                              "completion_tokens": 2}}

        saved = REGISTRY._specs["meta"]
        REGISTRY._specs["meta"] = ProviderSpec(
            "meta", lambda: MetaAdapter(model="muse-spark-1.3",
                                        api_key="k", post=fake),
            "test fake")
        self.addCleanup(
            lambda: REGISTRY._specs.__setitem__("meta", saved))
        project = new_project("p1", "g")
        task = new_task(project, "t1", "test", "reply OK")
        result = run_task(
            self.conn, task,
            messages=[{"role": "user", "content": "Reply with exactly: OK"}],
            conv_id=1, user_id=1, provider_name="meta",
            max_attempts=1, fallback_to_mock=False, max_tokens=64)
        self.assertEqual(result.provider, "meta")
        self.assertEqual(seen[0]["max_tokens"], 64)

    def test_run_task_records_diagnostic_notes(self):
        from app.ai.registry import ProviderSpec

        def fake(url, payload, headers):
            return {"choices": [{"finish_reason": "stop",
                                 "message": {"content": "OK"}}],
                    "usage": {"prompt_tokens": 10,
                              "completion_tokens": 200,
                              "completion_tokens_details": {
                                  "reasoning_tokens": 180}}}

        saved = REGISTRY._specs["meta"]
        REGISTRY._specs["meta"] = ProviderSpec(
            "meta", lambda: MetaAdapter(model="muse-spark-1.3",
                                        api_key="k", post=fake),
            "test fake")
        self.addCleanup(
            lambda: REGISTRY._specs.__setitem__("meta", saved))
        project = new_project("p1", "g")
        task = new_task(project, "t1", "chat", "hi")
        result = run_task(self.conn, task, messages=[],
                          conv_id=1, user_id=1, provider_name="meta",
                          max_attempts=1, fallback_to_mock=False)
        self.assertEqual(result.reasoning_tokens, 180)
        self.assertEqual(result.finish_reason, "stop")
        notes = self.conn.execute(
            "SELECT notes FROM usage_log ORDER BY id DESC LIMIT 1"
        ).fetchone()["notes"]
        self.assertIn("finish_reason=stop", notes)
        self.assertIn("reasoning_tokens=180", notes)

    def test_run_task_max_tokens_refused_off_meta_text_path(self):
        project = new_project("p1", "g")
        task = new_task(project, "t1", "chat", "hi")
        # Mock provider: refusing loudly beats silently ignoring it.
        with self.assertRaises(ValueError):
            run_task(self.conn, task, messages=[],
                     conv_id=1, user_id=1, provider_name="mock",
                     max_tokens=64)
        # Tools path: routing itself refuses mock+tools first -- either
        # way the combination is refused loudly with no network call.
        with self.assertRaises(ProviderNotConfigured):
            run_task(self.conn, task, messages=[],
                     conv_id=1, user_id=1, provider_name="mock",
                     tools=[{"name": "t"}],
                     tool_handler=lambda n, a: {},
                     max_tokens=64)
        with self.assertRaises(ValueError):
            run_task(self.conn, task, messages=[],
                     conv_id=1, user_id=1, provider_name="mock",
                     structured_schema={"type": "object"},
                     max_tokens=64)
        # Meta + tools: routing succeeds, so the override guard itself
        # must refuse (no tool-loop call may carry a text-only knob).
        from app.ai.registry import ProviderSpec

        def boom(url, payload, headers):
            raise AssertionError("no network on refused override")

        saved = REGISTRY._specs["meta"]
        REGISTRY._specs["meta"] = ProviderSpec(
            "meta", lambda: MetaAdapter(model="muse-spark-1.3",
                                        api_key="k", post=boom),
            "test fake")
        self.addCleanup(
            lambda: REGISTRY._specs.__setitem__("meta", saved))
        with self.assertRaises(ValueError):
            run_task(self.conn, task,
                     messages=[{"role": "user", "content": "hi"}],
                     conv_id=1, user_id=1, provider_name="meta",
                     tools=[{"name": "t"}],
                     tool_handler=lambda n, a: {},
                     max_tokens=64)
        left = self.conn.execute(
            "SELECT COUNT(*) AS n FROM usage_log").fetchone()
        self.assertEqual(left["n"], 0)


class TestStrictVendorUsage(unittest.TestCase):
    """Non-Meta adapters must never report unknown usage as (0, 0).

    Missing, null, or malformed usage raises ProviderError with
    unknown tokens, so run_task accounts the call through the
    failed_unknown estimate. Explicitly reported zeros stay known.
    """

    def _cases(self):
        return [
            ("openai", OpenAIAdapter(model="m", api_key="k"),
             lambda usage: {"choices": [{"message": {
                 "content": "Yo."}}], "usage": usage},
             "prompt_tokens", "completion_tokens"),
            ("gemini", GeminiAdapter(model="gem", api_key="k"),
             lambda usage: {"candidates": [{"content": {"parts": [
                 {"text": "Hi"}]}}], "usageMetadata": usage},
             "promptTokenCount", "candidatesTokenCount"),
            ("anthropic", AnthropicAdapter(model="m", api_key="k"),
             lambda usage: {"content": [{"type": "text",
                                         "text": "Hi"}],
                            "usage": usage},
             "input_tokens", "output_tokens"),
        ]

    def test_missing_usage_raises_unknown(self):
        for vendor, adapter, body, _in, _out in self._cases():
            with self.subTest(vendor=vendor):
                adapter._post = lambda u, p, h: body({"other": 1})
                with self.assertRaises(ProviderError) as ctx:
                    adapter.generate_text([])
                self.assertFalse(ctx.exception.usage_known)

    def test_null_and_malformed_usage_raise_unknown(self):
        bad_blocks = [None, [10, 5], "10/5", 42,
                      {"in": -5, "out": 10}, {"in": 10, "out": -1},
                      {"in": True, "out": 5}, {"in": "lots",
                                               "out": 5}]
        for vendor, adapter, body, in_key, out_key in self._cases():
            for bad in bad_blocks:
                if isinstance(bad, dict):
                    bad = {in_key: bad["in"], out_key: bad["out"]}
                with self.subTest(vendor=vendor, usage=bad):
                    adapter._post = lambda u, p, h, b=bad: body(b)
                    with self.assertRaises(ProviderError) as ctx:
                        adapter.generate_text([])
                    self.assertFalse(ctx.exception.usage_known)

    def test_valid_usage_parsed(self):
        for vendor, adapter, body, in_key, out_key in self._cases():
            with self.subTest(vendor=vendor):
                usage = {in_key: 11, out_key: 4}
                adapter._post = lambda u, p, h: body(usage)
                out = adapter.generate_text([])
                self.assertEqual((out.tokens_in, out.tokens_out),
                                 (11, 4))

    def test_explicit_zero_usage_is_known(self):
        for vendor, adapter, body, in_key, out_key in self._cases():
            with self.subTest(vendor=vendor):
                usage = {in_key: 0, out_key: 0}
                adapter._post = lambda u, p, h: body(usage)
                out = adapter.generate_text([])
                self.assertEqual((out.tokens_in, out.tokens_out),
                                 (0, 0))


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
        # v14 adds twin_chats (Find Something history).
        self.assertEqual(SCHEMA_VERSION, 14)


class TestBackCompat(unittest.TestCase):
    def test_legacy_router_preserved(self):
        from app.providers import (
            MockProvider,
            ProviderResult,
            get_provider,
        )

        # Pin ambient config: the default-provider assertion must not
        # depend on the developer's local AI_PROVIDER.
        with mock.patch("app.providers.settings", ai_provider="mock"):
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
