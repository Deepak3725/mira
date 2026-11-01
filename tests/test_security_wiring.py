"""Deploy-prep wiring tests: CSRF coverage, production guards (no HTTP)."""

import re
import unittest
from pathlib import Path
from unittest import mock

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
        good = mock.Mock(app_env="production", session_secret="x" * 40)
        with mock.patch("app.main.settings", good):
            check_production_settings()

    def test_seed_refuses_production(self):
        from app.seed import refuse_in_production

        refuse_in_production()  # dev defaults pass
        prod = mock.Mock(app_env="production")
        with mock.patch("app.config.settings", prod):
            self.assertRaises(SystemExit, refuse_in_production)


if __name__ == "__main__":
    unittest.main()
