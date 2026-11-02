"""Deploy-prep wiring tests: CSRF coverage, production guards (no HTTP)."""

import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from starlette.requests import Request

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"
POST_FORM = re.compile(r'<form[^>]*?method="post"[^>]*?>', re.DOTALL)
CSRF_INPUT = 'name="csrf_token"'


class TestCsrfTemplateCoverage(unittest.TestCase):
    def test_every_post_form_carries_token(self):
        missing = []
        count = 0
        for path in sorted(TEMPLATES.glob("*.html")):
            text = path.read_text(encoding="utf-8")
            for match in POST_FORM.finditer(text):
                count += 1
                end = text.find("</form>", match.end())
                body = text[match.end():end if end != -1 else len(text)]
                if CSRF_INPUT not in body:
                    missing.append(f"{path.name}:{match.group(0)[:60]}")
        self.assertGreaterEqual(count, 20)
        self.assertEqual(missing, [])

    def test_every_post_route_validates_token(self):
        routes = Path(__file__).resolve().parent.parent / "app" / "routes"
        missing = []
        for path in sorted(routes.glob("*.py")):
            lines = path.read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines):
                if line.startswith("@router.post"):
                    block = "\n".join(lines[i:i + 40])
                    # Vendor webhooks cannot present session CSRF tokens;
                    # they authenticate with HMAC signatures instead.
                    # Logged-in forms use session-bound CSRF.
                    if ("require_csrf" not in block
                            and "require_session_csrf" not in block
                            and "_act(" not in block
                            and "verify_webhook" not in block):
                        missing.append(f"{path.name}:{i + 1}")
        self.assertEqual(missing, [])


class TestTwinFormPrivacyDefaults(unittest.TestCase):
    def test_new_twin_shares_nothing_by_default(self):
        from app.routes.accounts import _twin_form_context

        form = _twin_form_context()
        self.assertEqual(form["authorized"], [])

    def test_edit_form_shows_stored_allowlist_verbatim(self):
        from app.routes.accounts import _twin_form_context

        twin = {"display_name": "T", "persona": "p", "tone": "t",
                "is_public": 0, "profile_json": "{}",
                "authorized_fields_json": '["skills"]',
                "conditions_json": "{}"}
        self.assertEqual(_twin_form_context(twin)["authorized"],
                         ["skills"])
        twin["authorized_fields_json"] = "[]"
        self.assertEqual(_twin_form_context(twin)["authorized"], [])


class TestLoginUnknownAccount(unittest.TestCase):
    def test_same_error_without_timing_shortcut(self):
        import os

        from app import auth as auth_mod
        from app import config as config_mod
        from app import db as db_mod
        from app.routes import accounts as accounts_route

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "login.db")
        db_mod.init_db(db)
        saved_env = os.environ.get("DATABASE_PATH")
        os.environ["DATABASE_PATH"] = db

        def _restore_env():
            if saved_env is None:
                os.environ.pop("DATABASE_PATH", None)
            else:
                os.environ["DATABASE_PATH"] = saved_env

        self.addCleanup(_restore_env)
        saved = db_mod.settings
        db_mod.settings = config_mod.get_settings()
        self.addCleanup(setattr, db_mod, "settings", saved)
        conn = db_mod.get_connection(db)
        try:
            db_mod.create_user(
                conn, name="U", role="professional",
                email="u@login.test",
                password_hash=auth_mod.hash_password("correct-horse-1"))
            conn.commit()
        finally:
            conn.close()

        def attempt(email, password):
            req = Request({
                "type": "http", "http_version": "1.1", "method": "POST",
                "scheme": "http", "path": "/login",
                "raw_path": b"/login", "query_string": b"",
                "headers": [(b"host", b"testserver")],
                "server": ("testserver", 80),
                "client": ("testclient", 50000)})
            token = auth_mod.csrf_token(None)
            return accounts_route.login(
                req, email=email, password=password, csrf_token=token)

        # Unknown account, wrong password, right password.
        self.assertEqual(attempt("nobody@login.test", "x" * 9).status_code,
                         400)
        self.assertEqual(
            attempt("u@login.test", "wrong-pass-9").status_code, 400)
        resp = attempt("u@login.test", "correct-horse-1")
        self.assertEqual(resp.status_code, 303)
        # The dummy hash parses and never verifies.
        self.assertFalse(auth_mod.verify_password(
            "anything-9", auth_mod.DUMMY_HASH))


class TestProductionGuards(unittest.TestCase):
    def test_startup_refuses_dev_secret_in_production(self):
        from app.main import check_production_settings

        check_production_settings()  # dev defaults pass
        bad = mock.Mock(app_env="production",
                        session_secret="dev-only-insecure-secret-change-me")
        with mock.patch("app.main.settings", bad):
            self.assertRaises(RuntimeError, check_production_settings)
        short = mock.Mock(app_env="production", session_secret="short")
        with mock.patch("app.main.settings", short):
            self.assertRaises(RuntimeError, check_production_settings)
        # Documented boundary: 32 chars is the enforced minimum.
        almost = mock.Mock(app_env="production",
                           session_secret="x" * 31)
        with mock.patch("app.main.settings", almost):
            self.assertRaises(RuntimeError, check_production_settings)
        minimum = mock.Mock(app_env="production",
                            session_secret="x" * 32,
                            database_url="postgresql://db/mira")
        with mock.patch("app.main.settings", minimum):
            check_production_settings()
        good = mock.Mock(app_env="production", session_secret="x" * 40)
        with mock.patch("app.main.settings", good):
            check_production_settings()

    def test_startup_refuses_missing_database_url_in_production(self):
        from app.main import check_production_settings

        check_production_settings()  # dev defaults pass (SQLite allowed)
        for blank in ("", "   "):
            prod = mock.Mock(app_env="production",
                             session_secret="x" * 40,
                             database_url=blank)
            with mock.patch("app.main.settings", prod):
                self.assertRaises(RuntimeError,
                                  check_production_settings)
        prod = mock.Mock(app_env="production",
                         session_secret="x" * 40,
                         database_url="postgresql://db/mira")
        with mock.patch("app.main.settings", prod):
            check_production_settings()
        dev = mock.Mock(app_env="dev", session_secret="anything",
                        database_url="")
        with mock.patch("app.main.settings", dev):
            check_production_settings()

    def test_seed_refuses_production(self):
        from app.seed import refuse_in_production

        refuse_in_production()  # dev defaults pass
        prod = mock.Mock(app_env="production")
        with mock.patch("app.config.settings", prod):
            self.assertRaises(SystemExit, refuse_in_production)


if __name__ == "__main__":
    unittest.main()
