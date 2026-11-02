"""Safe application configuration.

Reads settings from environment variables, optionally loaded from a local
``.env`` file (parsed with the standard library only, so Phase 0 needs no
extra dependency for this). Secrets are never printed, logged, or returned
by any endpoint.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ (no overwrite)."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    app_name: str = "MIRA"
    app_env: str = "dev"
    host: str = "127.0.0.1"
    port: int = 8000
    database_path: str = "data/mira.db"
    # When set, PostgreSQL is used instead of SQLite (production).
    # Local/tests keep SQLite unless MIRA_TEST_DATABASE_URL is set.
    database_url: str = ""
    # Optional; unused until Phase 3B verifies safe Meta usage. Never log this.
    model_api_key: str = ""
    # Other vendors: API keys plus explicit model ids. Empty model/key
    # means that vendor is unavailable (adapters refuse). Never log these.
    openai_api_key: str = ""
    openai_model: str = ""
    gemini_api_key: str = ""
    gemini_model: str = ""
    anthropic_api_key: str = ""
    anthropic_model: str = ""
    # Optional task routing: "kind:provider,kind:provider" (e.g.
    # "code:openai,intent:meta"). Unknown pairs are ignored.
    ai_routes: str = ""
    # HMAC key for session cookies. Set a long random SESSION_SECRET in .env;
    # the default is for local demo only and must never be used in production.
    session_secret: str = "dev-only-insecure-secret-change-me"
    # Reply provider: "mock" (default, $0) or "meta" (only after 3B checks).
    ai_provider: str = "mock"
    # Verified Meta model id. Empty means unverified: real calls are refused.
    meta_model: str = ""
    # Hard spending caps in USD. Inference stops when any cap is reached.
    budget_monthly_usd: float = 75.0
    budget_per_conversation_usd: float = 1.0
    budget_per_user_daily_usd: float = 5.0
    # Demo mode (MIRA_DEMO_MODE=1): allow demo seeding and documented demo
    # credentials. Off by default everywhere; enable only on an isolated
    # demo/development database, never on a production user database.
    demo_mode: bool = False
    # Deadline/match-queue sweeper (MIRA_WORKER=0 disables). Inline
    # post-save matching never depends on it; only expirations and
    # failed-event retries do. Single-process only: run one worker.
    worker_enabled: bool = True
    worker_interval_seconds: int = 30


def get_settings() -> Settings:
    _load_dotenv(ROOT_DIR / ".env")
    return Settings(
        app_name=os.environ.get("APP_NAME", "MIRA"),
        app_env=os.environ.get("APP_ENV", "dev"),
        host=os.environ.get("HOST", "127.0.0.1"),
        port=_int_env("PORT", 8000),
        database_path=os.environ.get("DATABASE_PATH", "data/mira.db"),
        database_url=os.environ.get("DATABASE_URL", ""),
        model_api_key=os.environ.get("MODEL_API_KEY", ""),
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        openai_model=os.environ.get("OPENAI_MODEL", ""),
        gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
        gemini_model=os.environ.get("GEMINI_MODEL", ""),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", ""),
        ai_routes=os.environ.get("AI_ROUTES", ""),
        session_secret=os.environ.get("SESSION_SECRET", "dev-only-insecure-secret-change-me"),
        ai_provider=os.environ.get("AI_PROVIDER", "mock"),
        meta_model=os.environ.get("META_MODEL", ""),
        budget_monthly_usd=_float_env("BUDGET_MONTHLY_USD", 75.0),
        budget_per_conversation_usd=_float_env("BUDGET_PER_CONVERSATION_USD", 1.0),
        budget_per_user_daily_usd=_float_env("BUDGET_PER_USER_DAILY_USD", 5.0),
        demo_mode=_bool_env("MIRA_DEMO_MODE", False),
        worker_enabled=_bool_env("MIRA_WORKER", True),
        worker_interval_seconds=_int_env("MIRA_WORKER_INTERVAL", 30),
    )


settings = get_settings()
