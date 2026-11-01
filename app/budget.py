"""Cost tracking and hard budget enforcement (Phase 3B).

Every provider call flows through `guarded_generate`, which:
  1. Refuses Meta calls when monthly / per-user-daily / per-conversation
     caps are already reached (raises BudgetExhausted).
  2. Records input tokens, output tokens, model, and estimated cost per call.

Prices below are Standard-mode rates copied from the official Meta Model API
pricing page (https://dev.meta.ai/docs/pricing-rate-limits) on 2026-10-30.
Unknown models are refused rather than billed blindly.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from sqlite3 import Connection
from typing import Any

from .config import settings

# model_id -> (input USD per 1M tokens, output USD per 1M tokens), Standard mode.
PRICES: dict[str, tuple[float, float]] = {
    "muse-spark-1.3": (1.25, 4.25),
    "muse-spark-1.2": (1.25, 4.25),
    "muse-spark-1.1": (1.25, 4.25),
}


class BudgetExhausted(Exception):
    """A spending cap was reached. No paid call may proceed."""


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    if model not in PRICES:
        raise BudgetExhausted(f"No verified pricing for model {model!r}.")
    price_in, price_out = PRICES[model]
    return tokens_in / 1_000_000 * price_in + tokens_out / 1_000_000 * price_out


#: Verified per-vendor prices: (provider, model) -> (in $/1M, out $/1M).
#: Starts empty: add an entry only after checking the vendor's official
#: pricing page, with a dated comment like PRICES above. Unpriced
#: vendor/model pairs are refused rather than billed blindly.
VENDOR_PRICES: dict[tuple[str, str], tuple[float, float]] = {}


def estimate_cost_for(provider: str, model: str,
                      tokens_in: int, tokens_out: int) -> float:
    """Price a call on any provider. Mock is $0; unknown is refused."""
    if provider == "mock":
        return 0.0
    if provider == "meta":
        return estimate_cost(model, tokens_in, tokens_out)
    key = (provider, model)
    if key not in VENDOR_PRICES:
        raise BudgetExhausted(
            f"No verified pricing for {provider}/{model}. Verify the"
            " vendor's pricing page, then add it to VENDOR_PRICES.")
    price_in, price_out = VENDOR_PRICES[key]
    return tokens_in / 1_000_000 * price_in + tokens_out / 1_000_000 * price_out


def _spent(conn: Connection, where: str, params: tuple) -> float:
    row = conn.execute(
        f"SELECT COALESCE(SUM(est_cost_usd), 0) AS total FROM usage_log WHERE {where}",
        params,
    ).fetchone()
    return float(row["total"])


def _stamp(moment: dt.datetime) -> str:
    # Space-separated UTC ("YYYY-MM-DD HH:MM:SS"): string-comparable with
    # historical SQLite rows and parsed by PostgreSQL in the session's
    # UTC time zone (see PGConnection).
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _month_bounds() -> tuple[str, str]:
    now = dt.datetime.now(dt.timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = (start + dt.timedelta(days=32)).replace(day=1)
    return _stamp(start), _stamp(nxt)


def _day_bounds() -> tuple[str, str]:
    now = dt.datetime.now(dt.timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return _stamp(start), _stamp(start + dt.timedelta(days=1))


def monthly_spend(conn: Connection) -> float:
    start, end = _month_bounds()
    return _spent(conn, "created_at >= ? AND created_at < ?", (start, end))


def user_daily_spend(conn: Connection, user_id: int | None) -> float:
    start, end = _day_bounds()
    return _spent(conn, "user_id IS ? AND created_at >= ? AND created_at < ?",
                   (user_id, start, end))


def conversation_spend(conn: Connection, conv_id: int | None) -> float:
    return _spent(conn, "conv_id IS ?", (conv_id,))


def check_budgets(conn: Connection, user_id: int | None, conv_id: int | None) -> None:
    if monthly_spend(conn) >= settings.budget_monthly_usd:
        raise BudgetExhausted(
            f"Monthly AI budget reached (${settings.budget_monthly_usd:.2f}).")
    if user_daily_spend(conn, user_id) >= settings.budget_per_user_daily_usd:
        raise BudgetExhausted(
            f"Daily per-user AI budget reached (${settings.budget_per_user_daily_usd:.2f}).")
    if conversation_spend(conn, conv_id) >= settings.budget_per_conversation_usd:
        raise BudgetExhausted(
            f"Per-conversation AI budget reached (${settings.budget_per_conversation_usd:.2f}).")


# usage_log.status values. "failed_unknown" rows store 0 tokens / $0
# numerically, but the status + notes make explicit that the real usage is
# unknown and may have been billed -- never read them as genuinely free.
STATUSES = ("ok", "failed_known", "failed_unknown")


def record_usage(conn: Connection, *, provider: str, model: str,
                 conv_id: int | None, user_id: int | None,
                 tokens_in: int, tokens_out: int,
                 status: str = "ok", notes: str = "",
                 latency_ms: int = 0, attempts: int = 1) -> float:
    if status not in STATUSES:
        raise ValueError(f"Unknown usage status {status!r}.")
    if status == "failed_unknown":
        tokens_in, tokens_out, cost = 0, 0, 0.0
    else:
        cost = estimate_cost_for(provider, model, tokens_in, tokens_out)
    conn.execute(
        "INSERT INTO usage_log (provider, model, conv_id, user_id,"
        " tokens_in, tokens_out, est_cost_usd, status, notes,"
        " latency_ms, attempts)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (provider, model, conv_id, user_id, tokens_in, tokens_out, cost,
         status, notes[:300], latency_ms, attempts),
    )
    # Usage must survive even when the caller never commits (e.g. the
    # request-review page records intent usage, then only closes).
    conn.commit()
    return cost


def guarded_generate(conn: Connection, provider, context, *,
                     conv_id: int | None, user_id: int | None):
    """Generate one reply with budget checks + usage recording."""
    if provider.name == "mock":
        result = provider.generate(context)
        record_usage(conn, provider="mock", model="mock", conv_id=conv_id,
                     user_id=user_id, tokens_in=0, tokens_out=0)
        return result
    check_budgets(conn, user_id, conv_id)
    result = provider.generate(context)
    record_usage(conn, provider=provider.name, model=provider.model,
                 conv_id=conv_id, user_id=user_id,
                 tokens_in=result.tokens_in, tokens_out=result.tokens_out)
    return result


def user_monthly_spend(conn: Connection, user_id: int | None) -> float:
    start, end = _month_bounds()
    return _spent(conn, "user_id IS ? AND created_at >= ? AND created_at < ?",
                   (user_id, start, end))


def usage_summary(conn: Connection, user_id: int | None = None) -> dict[str, Any]:
    """Aggregate usage. With user_id: that user's rows only ('mine').

    Without user_id the totals stay global ('all'); only aggregate-safe
    callers (tests, future admin views) may use the global form -- the
    /usage page always passes the viewer's id.
    """
    filt = "" if user_id is None else "WHERE user_id IS ?"
    params: tuple = () if user_id is None else (user_id,)
    row = conn.execute(
        "SELECT COUNT(*) AS requests, COALESCE(SUM(tokens_in), 0) AS tokens_in,"
        " COALESCE(SUM(tokens_out), 0) AS tokens_out,"
        " COALESCE(SUM(est_cost_usd), 0) AS est_cost_usd,"
        f" COALESCE(AVG(latency_ms), 0) AS avg_latency_ms FROM usage_log {filt}",
        params,
    ).fetchone()
    by_model = [
        {"model": r["model"], "requests": r["requests"],
         "tokens_in": r["tokens_in"], "tokens_out": r["tokens_out"],
         "est_cost_usd": round(r["est_cost_usd"], 6),
         "avg_latency_ms": int(r["avg_latency_ms"] or 0)}
        for r in conn.execute(
            "SELECT model, COUNT(*) AS requests, SUM(tokens_in) AS tokens_in,"
            " SUM(tokens_out) AS tokens_out, SUM(est_cost_usd) AS est_cost_usd,"
            " AVG(latency_ms) AS avg_latency_ms"
            f" FROM usage_log {filt} GROUP BY model ORDER BY model",
            params,
        )
    ]
    by_status = [
        {"status": r["status"], "requests": r["requests"],
         "est_cost_usd": round(r["est_cost_usd"] or 0, 6)}
        for r in conn.execute(
            "SELECT status, COUNT(*) AS requests,"
            " COALESCE(SUM(est_cost_usd), 0) AS est_cost_usd"
            f" FROM usage_log {filt} GROUP BY status ORDER BY status",
            params,
        )
    ]
    if user_id is None:
        month = monthly_spend(conn)
    else:
        month = user_monthly_spend(conn, user_id)
    return {
        "scope": "all" if user_id is None else "mine",
        "requests": row["requests"], "tokens_in": row["tokens_in"],
        "tokens_out": row["tokens_out"], "est_cost_usd": round(row["est_cost_usd"], 6),
        "avg_latency_ms": int(row["avg_latency_ms"] or 0),
        "monthly_spend": round(month, 6),
        "monthly_cap": settings.budget_monthly_usd,
        "by_model": by_model,
        "by_status": by_status,
    }
