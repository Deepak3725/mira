"""Test-package bootstrap: keep the unit suite hermetic and offline-safe.

Forces AI_PROVIDER=mock for every test process and strips ambient live
credentials (API keys, models, route maps) before application config
loads, so no unit test can depend on ambient provider configuration or
reach a live model (units must never bill). Meta code paths are covered
by tests that pass explicit providers with injected fake transports.

SAFE INVOCATION (from the repo root only):

    $env:MIRA_TEST_DATABASE_URL='sqlite'
    D:\\Others\\Mira\\.python\\python.exe -m unittest tests.test_<name> [...]

Run tests ONLY as ``tests.*`` package modules: importing the ``tests``
package is what installs this guard. Do NOT run test files directly as
scripts (``python tests/test_x.py``) -- that bypasses this module, the
ambient AI_PROVIDER applies, and provider-touching tests may bill or
fail. ``tests/test_readiness.py`` asserts the forced mock selection.

Test temp files are redirected to the repo-local ``.tmp_tests/`` directory
(gitignored): sandboxes that forbid system-temp writes make ``tempfile``
fall back to the current working directory, which littered the repo root
with ``tmp*`` directories. ``TemporaryDirectory`` names stay unique, so
parallel runs are safe; remove ``.tmp_tests/`` manually to reclaim space.
"""

import os
import tempfile
from pathlib import Path

_test_tmp = Path(__file__).resolve().parent.parent / ".tmp_tests"
_test_tmp.mkdir(exist_ok=True)
tempfile.tempdir = str(_test_tmp)

# Marks this process as a test run so app.db honors MIRA_TEST_DATABASE_URL
# here (Postgres suite) while the real server always ignores it. See
# tests/test_pg_adapter.py::TestDatabaseRedirectGuard for the regression.
os.environ["MIRA_TEST_MODE"] = "1"
os.environ["AI_PROVIDER"] = "mock"
for _var in ("MODEL_API_KEY", "META_MODEL", "AI_ROUTES",
             "OPENAI_API_KEY", "OPENAI_MODEL",
             "GEMINI_API_KEY", "GEMINI_MODEL",
             "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
    os.environ.pop(_var, None)
