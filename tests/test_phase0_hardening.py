"""Phase 0 hardening tests: privacy scoping, boot/docs, migrations,
SQLite pragmas, rate-limit wiring, password caps, body limits, upload
validation, redacting logs, retention, and fixture pinning."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import budget
from app.db import (
    _ENSURE_COLUMNS,
    _ensure_column,
    driver_name,
    get_connection,
    init_db,
)

HERE = Path(__file__).resolve().parent
REPO = HERE.parent


class HardeningCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.db = str(Path(self._td.name) / "h0.db")
        init_db(self.db)
        self.conn = get_connection(self.db)
        self.driver = driver_name(self.conn)

    def tearDown(self):
        self.conn.close()
        self._td.cleanup()

    def _columns(self, table):
        if self.driver == "postgres":
            rows = self.conn.execute(
                "SELECT column_name AS name FROM information_schema.columns"
                " WHERE table_schema = current_schema() AND table_name = ?",
                (table,),
            )
        else:
            rows = self.conn.execute(f"PRAGMA table_info({table})")
        return {r["name"] for r in rows}


class TestUsageScoping(HardeningCase):
    def _seed(self):
        budget.record_usage(
            self.conn, provider="meta", model="muse-spark-1.3",
            conv_id=1, user_id=1, tokens_in=1000, tokens_out=500)
        budget.record_usage(
            self.conn, provider="meta", model="muse-spark-1.3",
            conv_id=2, user_id=2, tokens_in=100, tokens_out=50)

    def test_summary_scoped_to_user(self):
        self._seed()
        one = budget.usage_summary(self.conn, 1)
        two = budget.usage_summary(self.conn, 2)
        self.assertEqual(one["requests"], 1)
        self.assertEqual((one["tokens_in"], one["tokens_out"]), (1000, 500))
        self.assertEqual(two["requests"], 1)
        self.assertEqual((two["tokens_in"], two["tokens_out"]), (100, 50))
        self.assertGreater(one["est_cost_usd"], two["est_cost_usd"])

    def test_global_summary_preserved(self):
        self._seed()
        total = budget.usage_summary(self.conn)
        self.assertEqual(total["requests"], 2)
        self.assertEqual(total["scope"], "all")
        self.assertEqual(budget.usage_summary(self.conn, 1)["scope"], "mine")


class TestDocsDisabled(unittest.TestCase):
    def test_docs_off_in_production(self):
        from app import main as main_mod

        with mock.patch.object(main_mod, "settings") as cfg:
            cfg.app_env = "production"
            kwargs = main_mod._docs_kwargs()
        self.assertEqual(kwargs.get("docs_url"), None)
        self.assertIn("docs_url", kwargs)
        self.assertIn("openapi_url", kwargs)

    def test_docs_on_outside_production(self):
        from app import main as main_mod

        with mock.patch.object(main_mod, "settings") as cfg:
            cfg.app_env = "dev"
            self.assertEqual(main_mod._docs_kwargs(), {})


class TestEnsureColumnUnified(HardeningCase):
    def test_specs_cover_known_columns(self):
        pairs = {(t, c) for t, c, _ in _ENSURE_COLUMNS}
        for pair in [("users", "password_hash"), ("twins", "is_public"),
                     ("twins", "conditions_json"),
                     ("opportunities", "category"),
                     ("opportunities", "attributes_json"),
                     ("conversations", "request_id"),
                     ("usage_log", "status"), ("usage_log", "notes"),
                     ("usage_log", "latency_ms"), ("usage_log", "attempts")]:
            self.assertIn(pair, pairs)

    def test_ensure_column_idempotent_both_drivers(self):
        self.conn.execute("CREATE TABLE probe (x TEXT)")
        _ensure_column(self.conn, "probe", "y", "TEXT")
        _ensure_column(self.conn, "probe", "y", "TEXT")
        self.conn.commit()
        self.assertEqual(self._columns("probe"), {"x", "y"})


class TestSqlitePragmas(HardeningCase):
    def test_wal_and_busy_timeout(self):
        if self.driver == "postgres":
            self.skipTest("SQLite-only pragmas")
        mode = self.conn.execute("PRAGMA journal_mode").fetchone()[0]
        timeout = self.conn.execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertEqual(str(mode).lower(), "wal")
        self.assertEqual(timeout, 5000)


def _handler_source(path, name):
    text = (REPO / path).read_text(encoding="utf-8")
    start = text.find(f"def {name}(")
    if start < 0:
        return ""
    rest = text[start:]
    for marker in ("\n@router.", "\ndef ", "\nasync def "):
        cut = rest.find(marker, 1)
        if cut > 0:
            rest = rest[:cut]
    return rest


class TestRateLimitWiring(unittest.TestCase):
    LIMITED = [
        ("app/routes/accounts.py", "twin_create"),
        ("app/routes/accounts.py", "twin_update"),
        ("app/routes/accounts.py", "twin_delete"),
        ("app/routes/memories.py", "memory_import"),
        ("app/routes/memories.py", "memory_approve"),
        ("app/routes/memories.py", "memory_reject"),
        ("app/routes/memories.py", "memory_delete"),
        ("app/routes/requests.py", "request_confirm"),
        ("app/routes/requests.py", "request_close"),
    ]

    def test_write_handlers_are_rate_limited(self):
        for path, name in self.LIMITED:
            with self.subTest(handler=name):
                self.assertIn("check_rate_limit", _handler_source(path, name))

    def test_conversation_controls_are_rate_limited(self):
        body = _handler_source("app/routes/chats.py", "_act")
        self.assertIn("check_rate_limit", body)
        # step/auto already had a limiter; other actions must have one too.
        self.assertGreaterEqual(body.count("check_rate_limit"), 2)

    def test_import_has_per_user_quota(self):
        body = _handler_source("app/routes/memories.py", "memory_import")
        self.assertIn("memimport", body)


class TestPasswordMax(unittest.TestCase):
    def test_max_length_constant(self):
        from app import auth

        self.assertEqual(auth.MAX_PASSWORD_LENGTH, 128)

    def test_register_and_login_enforce_max(self):
        text = (REPO / "app/routes/accounts.py").read_text(encoding="utf-8")
        self.assertIn("MAX_PASSWORD_LENGTH", text)
        self.assertGreaterEqual(text.count("MAX_PASSWORD_LENGTH"), 2)


def _drive(coro):
    """Drive an I/O-free coroutine to completion without an event loop.

    asyncio.run() needs a socketpair, which sandboxes block; the body
    limiter performs no I/O, so manual driving is exact.
    """
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("coroutine suspended on real I/O")


class TestBodyLimit(unittest.TestCase):
    def test_oversize_body_rejected_without_handler(self):
        from fastapi.responses import Response
        from starlette.requests import Request
        from app.main import MAX_BODY_BYTES, limit_body_size

        calls = []

        async def handler(request):
            calls.append(True)
            return Response("ok")

        big = Request({"type": "http", "method": "POST", "path": "/x",
                       "headers": [(b"content-length",
                                    str(MAX_BODY_BYTES + 1).encode())]})
        resp = _drive(limit_body_size(big, handler))
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(calls, [])

    def test_small_body_passes_through(self):
        from fastapi.responses import Response
        from starlette.requests import Request
        from app.main import limit_body_size

        async def handler(request):
            return Response("ok")

        small = Request({"type": "http", "method": "POST", "path": "/x",
                         "headers": [(b"content-length", b"16")]})
        resp = _drive(limit_body_size(small, handler))
        self.assertEqual(resp.status_code, 200)


class TestUploadAccept(unittest.TestCase):
    def test_accept_matrix(self):
        from app.import_pipeline import accept_upload

        self.assertIsNone(accept_upload("exp.json", "application/json"))
        self.assertIsNone(accept_upload("EXP.JSON", "text/json"))
        self.assertIsNone(accept_upload("exp.json", "text/plain"))
        self.assertIsNone(accept_upload("export.zip", "application/zip"))
        self.assertIsNone(
            accept_upload("EXPORT.ZIP", "application/x-zip-compressed"))
        self.assertEqual(accept_upload("exp.json", "image/png"), "bad-file")
        self.assertEqual(accept_upload("exp.exe", "application/json"), "bad-file")
        self.assertEqual(accept_upload("exp.json", ""), "bad-file")
        self.assertEqual(accept_upload("exp.zip", "application/json"), "bad-file")
        self.assertEqual(accept_upload("exp.zip", ""), "bad-file")
        self.assertEqual(accept_upload("", "application/json"), "no-file")


class TestLogging(unittest.TestCase):
    def test_redact_secrets(self):
        from app.log import redact

        cleaned = redact({"password": "hunter2", "name": "Ann",
                          "url": "https://u:p@host/db",
                          "nested": {"api_key": "k", "ok": 1}})
        self.assertEqual(cleaned["password"], "[REDACTED]")
        self.assertEqual(cleaned["name"], "Ann")
        self.assertNotIn("p@host", cleaned["url"])
        self.assertEqual(cleaned["nested"]["api_key"], "[REDACTED]")
        self.assertEqual(cleaned["nested"]["ok"], 1)

    def test_json_formatter_emits_parseable_log(self):
        import logging

        from app.log import JsonFormatter

        record = logging.LogRecord("mira", logging.INFO, __file__, 1,
                                   "hello", None, None)
        line = JsonFormatter().format(record)
        parsed = json.loads(line)
        self.assertEqual(parsed["message"], "hello")
        self.assertEqual(parsed["level"], "INFO")


class TestRetention(HardeningCase):
    def test_purge_old_rows(self):
        from app.retention import purge_old_rows

        self.conn.execute(
            "INSERT INTO usage_log (provider, model, tokens_in, tokens_out,"
            " est_cost_usd, created_at) VALUES ('mock', 'mock', 0, 0, 0,"
            " '2020-01-01 00:00:00')")
        self.conn.execute(
            "INSERT INTO usage_log (provider, model, tokens_in, tokens_out,"
            " est_cost_usd) VALUES ('mock', 'mock', 0, 0, 0)")
        self.conn.commit()
        removed = purge_old_rows(self.conn, "usage_log", 30)
        self.assertEqual(removed, 1)
        left = self.conn.execute("SELECT COUNT(*) AS n FROM usage_log").fetchone()
        self.assertEqual(left["n"], 1)

    def test_purge_rejects_unknown_tables(self):
        from app.retention import purge_old_rows

        with self.assertRaises(ValueError):
            purge_old_rows(self.conn, "users", 30)


class TestFixturePinned(unittest.TestCase):
    def test_sample_export_parses(self):
        from app.import_pipeline import parse_chatgpt_export

        raw = (HERE / "fixtures" / "sample_export.json").read_bytes()
        candidates = parse_chatgpt_export(raw)
        self.assertGreater(len(candidates), 0)
        for content, source in candidates:
            self.assertTrue(content.strip())
            self.assertTrue(source.strip())


if __name__ == "__main__":
    unittest.main()
