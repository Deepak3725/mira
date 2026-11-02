"""Evaluation orchestration: dry-run planner + gated live runner.

Usage (from the repo root; offline unless --live is given)::

    python -m app.eval.runner --dry-run --cases 5
    python -m app.eval.runner --live --max-cost-usd 2.00 --cases 5

Live mode additionally requires MIRA_EVAL_LIVE=1 in the environment.
Both keys must agree or the runner stays in dry-run and refuses to
call any provider. Live runs stop on the first of: max requests,
max cost, max errors, max total tokens, unexpected per-call cost,
or an unexpected exception. Retries and mock fallback are disabled
(max_attempts=1, fallback_to_mock=False) so live results measure
the real provider, never a mask.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time

from .. import budget as budget_engine
from ..ai.types import ProviderError, ProviderNotConfigured
from ..config import settings
from ..db import db_path, get_connection, init_db, test_database_url
from ..meta_client import MAX_CHAT_TOKENS, check_output_cap
from . import scenarios as S
from . import synth

DEFAULT_DB = "data/eval.db"
CHARS_PER_TOKEN = 4
#: Live multi-turn chats starved at the 300 default, so eval chat
#: calls request this cap explicitly (bounded, validated 1..1500).
DEFAULT_CHAT_MAX_TOKENS = 1500
STRUCTURED_OUTPUT_CAP = 4000
SCENARIO_REQUESTS = {"single_turn": 1, "multi_turn": None, "intent": 1,
                     "match_explanation": 0, "match_ranking": 0,
                     "privacy_probe": 1, "injection_probe": 1,
                     "concise_answer": 1}
TEXT_SCENARIOS = frozenset({"single_turn", "multi_turn",
                             "privacy_probe", "injection_probe",
                             "concise_answer"})
SCENARIO_OUTPUT_CAP = {"single_turn": None, "multi_turn": None,
                       "intent": STRUCTURED_OUTPUT_CAP,
                       "match_explanation": 0, "match_ranking": 0,
                       "privacy_probe": None, "injection_probe": None,
                       "concise_answer": None}

_EVAL_TABLES = (
    "CREATE TABLE IF NOT EXISTS eval_runs (id INTEGER PRIMARY KEY,"
    " started_at TEXT NOT NULL, mode TEXT NOT NULL, seed INTEGER NOT NULL,"
    " config_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL"
    " DEFAULT 'running', stop_reason TEXT NOT NULL DEFAULT '')",
    "CREATE TABLE IF NOT EXISTS eval_cases (id INTEGER PRIMARY KEY,"
    " run_id INTEGER NOT NULL REFERENCES eval_runs(id), scenario TEXT"
    " NOT NULL, idx INTEGER NOT NULL, ok INTEGER NOT NULL,"
    " metrics_json TEXT NOT NULL DEFAULT '{}', prompt_chars INTEGER"
    " NOT NULL DEFAULT 0, est_cost_usd REAL NOT NULL DEFAULT 0,"
    " live INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '')",
)


class EvalRefused(Exception):
    """Live preconditions failed. No provider call was made."""


def _stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _resolve_eval_db(path: str):
    from pathlib import Path

    target = Path(path)
    if not target.is_absolute():
        target = Path(__file__).resolve().parent.parent.parent / target
    if target.resolve() == db_path().resolve():
        raise EvalRefused(
            f"Refusing to run evaluation into the app database.")
    return target


def _spend(conn) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(est_cost_usd), 0) AS total FROM usage_log"
    ).fetchone()
    return float(row["total"])


def _tokens(conn) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(tokens_in), 0) + COALESCE(SUM(tokens_out), 0)"
        " AS total FROM usage_log").fetchone()
    return int(row["total"])


CHAT_SCENARIOS = frozenset({"single_turn", "multi_turn", "privacy_probe",
                            "injection_probe", "concise_answer"})
STRUCTURED_SCENARIOS = frozenset({"intent"})


def _usage_totals(conn, min_id: int = 0) -> tuple[int, int, float]:
    row = conn.execute(
        "SELECT COALESCE(SUM(tokens_in), 0) AS tin,"
        " COALESCE(SUM(tokens_out), 0) AS tout,"
        " COALESCE(SUM(est_cost_usd), 0) AS cost FROM usage_log"
        " WHERE id > ?", (min_id,)).fetchone()
    return int(row["tin"]), int(row["tout"]), float(row["cost"])


def _percentile(ordered: list[float], pct: int) -> float:
    if not ordered:
        return 0.0
    rank = min(len(ordered) - 1,
               max(0, int(pct / 100 * len(ordered))))
    return round(ordered[rank], 1)


def _max_usage_id(conn) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS top FROM usage_log").fetchone()
    return int(row["top"])


def _estimate_model() -> str:
    # Estimates need no ambient model: price against the documented
    # Standard model and label it as the estimate basis.
    if settings.meta_model in budget_engine.PRICES:
        return settings.meta_model
    return "muse-spark-1.3"


def _unknown_unit_estimate(model: str) -> float:
    """Conservative cost of one attempt billed without reported usage."""
    return budget_engine.estimate_cost_for(
        "meta", model, budget_engine.UNKNOWN_TOKENS_IN,
        budget_engine.UNKNOWN_TOKENS_OUT)


def _success_upper_bound(model: str, prompt_chars: int, cap: int,
                         calls: int) -> float:
    """Upper-bound success cost: full caps consumed on every call."""
    return budget_engine.estimate_cost_for(
        "meta", model, max(1, prompt_chars // CHARS_PER_TOKEN),
        cap * max(1, calls))


def _case_upper_bound(model: str, prompt_chars: int, cap: int,
                      calls: int) -> tuple[float, float, float]:
    """Worst-case cost for one case: (total, success_part, allowance).

    One attempt leaves one ledger row, so the worst case is the max of
    a maxed-out success and an unknown-usage failure -- never their
    sum. Both parts are reported separately from confirmed spend.
    """
    success = _success_upper_bound(model, prompt_chars, cap, calls)
    allowance = _unknown_unit_estimate(model) * max(1, calls)
    return max(success, allowance), success, allowance


def _planned_requests(args, selected: list[str]) -> int:
    total = 0
    for scenario in selected:
        expected = SCENARIO_REQUESTS[scenario]
        if expected is None:
            expected = args.turns
        total += expected * max(1, args.cases)
    return total


def _worst_per_request(model: str) -> float:
    """Prompt-independent per-request worst case for the pre-run gate."""
    from ..ai.router import MAX_ROUTER_TOTAL_CHARS

    unknown = _unknown_unit_estimate(model)
    success_ceiling = budget_engine.estimate_cost_for(
        "meta", model, MAX_ROUTER_TOTAL_CHARS // CHARS_PER_TOKEN,
        STRUCTURED_OUTPUT_CAP)
    return max(unknown, success_ceiling)


class Ctx:
    """Per-run scenario context: connections, caps, and dispatch."""

    def __init__(self, conn, args, run_id: int):
        self.conn = conn
        self.args = args
        self.run_id = run_id
        self.live = args.live
        self.requests = 0
        self.errors = 0
        self.latencies: list[float] = []
        self.stop_reason = ""
        self.model = settings.meta_model
        # Reporting scope: usage rows written by this run only. Cost
        # CAPS stay cumulative across the eval database (conservative).
        self.usage_start_id = _max_usage_id(conn)

    def chat_provider(self):
        from ..meta_client import MetaProvider
        from ..providers import MockProvider

        if not self.live:
            raise AssertionError("dry-run must not build providers")
        return MetaProvider()

    def intent_provider(self):
        return self.chat_provider()

    def _pre_request(self, count: int) -> None:
        if self.requests + count > self.args.max_requests:
            raise _Stop(f"max requests reached ({self.args.max_requests})")
        if _spend(self.conn) >= self.args.max_cost_usd:
            raise _Stop(f"max cost reached (${self.args.max_cost_usd:.2f})")
        if self.args.max_tokens and _tokens(self.conn) >= self.args.max_tokens:
            raise _Stop(f"max tokens reached ({self.args.max_tokens})")
        if self.errors >= self.args.max_errors:
            raise _Stop(f"max errors reached ({self.args.max_errors})")

    def complete(self, scenario, *, messages, prompt_chars, run,
               score) -> S.CaseResult:
        expected = SCENARIO_REQUESTS[scenario]
        if expected is None:  # multi_turn: turns come from args
            expected = self.args.turns
        if not self.live:
            cap = SCENARIO_OUTPUT_CAP[scenario]
            if cap is None:  # text scenarios use the chat cap
                cap = self.args.chat_max_tokens
            est_in = max(1, prompt_chars // CHARS_PER_TOKEN)
            est_model = _estimate_model()
            total, success, allowance = _case_upper_bound(
                est_model, prompt_chars, cap, expected)
            return S.CaseResult(
                scenario=scenario, ok=True, prompt_chars=prompt_chars,
                live=False,
                metrics={"planned_requests": expected,
                         "est_model": est_model,
                         "est_input_tokens": est_in,
                         "est_output_tokens": cap * max(1, expected),
                         "est_cost_usd": round(total, 6),
                         "est_success_usd": round(success, 6),
                         "est_unknown_allowance_usd": round(allowance,
                                                            6)})
        self._pre_request(expected)
        before = _usage_totals(self.conn, self.usage_start_id)
        start = time.perf_counter()
        try:
            out = run()
        except _Stop:
            raise
        except (ProviderError, ProviderNotConfigured,
                budget_engine.BudgetExhausted) as exc:
            self.errors += 1
            self.requests += expected
            return S.CaseResult(
                scenario=scenario, ok=False, prompt_chars=prompt_chars,
                live=True, error=f"{type(exc).__name__}: {exc}"[:300])
        latency_ms = (time.perf_counter() - start) * 1000
        self.latencies.append(latency_ms)
        self.requests += expected
        after = _usage_totals(self.conn, self.usage_start_id)
        delta = after[2] - before[2]
        ceiling = self.args.max_call_cost_usd * max(1, expected)
        if delta > ceiling:
            self.errors += 1
            raise _Stop(
                f"unexpected per-call cost ${delta:.4f} > ${ceiling:.4f}")
        result = score(out)
        result.live = True
        result.metrics["actual_tokens_in"] = after[0] - before[0]
        result.metrics["actual_tokens_out"] = after[1] - before[1]
        result.metrics["actual_cost_usd"] = round(delta, 6)
        if not result.ok:
            self.errors += 1
        return result

    def record(self, result: S.CaseResult, idx: int,
             est_cost: float = 0.0) -> None:
        self.conn.execute(
            "INSERT INTO eval_cases (run_id, scenario, idx, ok,"
            " metrics_json, prompt_chars, est_cost_usd, live, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.run_id, result.scenario, idx, 1 if result.ok else 0,
             json.dumps(result.metrics)[:2000], result.prompt_chars,
             est_cost, 1 if result.live else 0, result.error[:300]))
        self.conn.commit()


class _Stop(Exception):
    """A configured stop condition tripped. Not an error."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MIRA offline-first AI evaluation harness.")
    parser.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Plan only: estimate tokens/cost, call nothing (default).")
    parser.add_argument(
        "--live", action="store_true", default=False,
        help="Make real Meta calls. Also requires MIRA_EVAL_LIVE=1.")
    parser.add_argument("--db-path", default=DEFAULT_DB,
                        help="Eval database file (never the app DB).")
    parser.add_argument(
        "--scenarios", default=",".join(sorted(S.SCENARIOS)),
        help="Comma-separated subset to run.")
    parser.add_argument("--cases", type=int, default=5,
                        help="Cases per scenario.")
    parser.add_argument("--turns", type=int, default=4,
                        help="Turns for the multi_turn scenario.")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--max-requests", type=int, default=200)
    parser.add_argument("--max-cost-usd", type=float, default=0.0,
                        help="REQUIRED in live mode: hard spend cap.")
    parser.add_argument("--max-errors", type=int, default=5)
    parser.add_argument("--max-tokens", type=int, default=0,
                        help="Optional total-token budget (0 = off).")
    parser.add_argument("--max-call-cost-usd", type=float, default=0.05,
                        help="Stop if one call costs more than this.")
    parser.add_argument("--chat-max-tokens", type=int,
                        default=DEFAULT_CHAT_MAX_TOKENS,
                        help="Chat output cap, 1-1500 (default 1500).")
    return parser


def _check_live_preconditions(args) -> None:
    """Refuse live mode unless every precondition holds. No calls."""
    if os.environ.get("MIRA_TEST_DATABASE_URL", "").strip().lower() not in (
            "", "sqlite"):
        raise EvalRefused(
            "MIRA_TEST_DATABASE_URL must be empty or 'sqlite'.")
    _resolve_eval_db(args.db_path)  # refuses the app database path
    if not settings.meta_model or settings.meta_model not in (
            budget_engine.PRICES):
        raise EvalRefused("META_MODEL names no priced Standard model.")
    if "contributor" in settings.meta_model:
        raise EvalRefused("Contributor-mode models are refused.")
    if not settings.model_api_key:
        raise EvalRefused("MODEL_API_KEY is not configured.")
    if args.max_cost_usd <= 0:
        raise EvalRefused("--max-cost-usd is required in live mode.")


def _init_eval_db(path: str):
    _resolve_eval_db(path)  # never the app database, either mode
    init_db(path)
    conn = get_connection(path)
    for statement in _EVAL_TABLES:
        conn.execute(statement)
    conn.commit()
    return conn


def _selected_scenarios(args) -> list[str]:
    selected = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    unknown = [s for s in selected if s not in S.SCENARIOS]
    if unknown:
        raise EvalRefused(f"Unknown scenarios: {', '.join(unknown)}.")
    return selected


def _dispatch(ctx: Ctx, scenario: str, idx: int, case_no: int,
              twins, jobs, needs):
    # Global case number: every case gets distinct fixtures, so user
    # emails stay unique within and across scenarios of one run.
    pair = (twins[(2 * case_no) % len(twins)],
            twins[(2 * case_no + 1) % len(twins)])
    job = jobs[case_no % len(jobs)]
    if scenario == "single_turn":
        return S.scenario_single_turn(ctx, pair, job)
    if scenario == "multi_turn":
        return S.scenario_multi_turn(ctx, pair, job,
                                     turns=ctx.args.turns)
    if scenario == "intent":
        return S.scenario_intent(ctx, needs[case_no % len(needs)])
    # NOTE (hackathon compliance): no interview scenarios. Removed.
    if scenario == "match_explanation":
        return S.scenario_match_explanation(
            twins[(2 * case_no) % len(twins)], job)
    if scenario == "privacy_probe":
        return S.scenario_privacy_probe(
            ctx, twins[(2 * case_no) % len(twins)])
    if scenario == "injection_probe":
        return S.scenario_injection_probe(
            ctx, twins[(2 * case_no) % len(twins)])
    if scenario == "concise_answer":
        return S.scenario_concise_answer(
            ctx, twins[(2 * case_no) % len(twins)])
    if scenario == "match_ranking":
        return S.scenario_match_ranking(
            twins[(2 * case_no) % len(twins)], job)
    raise AssertionError(f"unreachable scenario {scenario}")


def _case_fell_back(scenario: str, metrics: dict, live: bool) -> bool:
    if not live:
        return False
    if scenario in CHAT_SCENARIOS:
        # Single-step chats record "provider"; multi-turn records
        # "providers". Either shape must detect a mock substitution.
        providers = metrics.get("providers")
        if providers is None:
            single = metrics.get("provider")
            providers = [single] if single else []
        return "mock" in providers
    if scenario in STRUCTURED_SCENARIOS:
        # Split cases join per-call sources with "+": only a non-meta
        # part counts as a fallback.
        sources = (metrics.get("source") or "meta").split("+")
        return any(part != "meta" for part in sources)
    return False


def _summarize(conn, run_id: int, ctx: Ctx | None) -> dict:
    live = ctx is not None and ctx.live
    start = ctx.usage_start_id if ctx is not None else 0
    rows = list(conn.execute(
        "SELECT scenario, ok, live, metrics_json FROM eval_cases"
        " WHERE run_id = ? ORDER BY id", (run_id,)))
    by_scenario: dict[str, dict] = {}
    groups = {"chat": {"cases": 0, "passed": 0, "cost_usd": 0.0,
                       "tokens_in": 0, "tokens_out": 0},
              "structured": {"cases": 0, "passed": 0, "cost_usd": 0.0,
                             "tokens_in": 0, "tokens_out": 0}}
    fallbacks = 0
    provider_cases = 0
    for row in rows:
        metrics = json.loads(row["metrics_json"] or "{}")
        entry = by_scenario.setdefault(
            row["scenario"], {"cases": 0, "ok": 0})
        entry["cases"] += 1
        entry["ok"] += 1 if row["ok"] else 0
        group = ("chat" if row["scenario"] in CHAT_SCENARIOS
                 else "structured"
                 if row["scenario"] in STRUCTURED_SCENARIOS else None)
        if group is not None:
            slot = groups[group]
            slot["cases"] += 1
            if row["ok"]:
                slot["passed"] += 1
            slot["cost_usd"] = round(
                slot["cost_usd"]
                + float(metrics.get("actual_cost_usd", 0.0)), 6)
            slot["tokens_in"] += int(metrics.get("actual_tokens_in", 0))
            slot["tokens_out"] += int(metrics.get("actual_tokens_out", 0))
        if group is not None and row["live"]:
            provider_cases += 1
            fallbacks += 1 if _case_fell_back(
                row["scenario"], metrics, True) else 0
    leaks = sum(
        len(json.loads(row["metrics_json"] or "{}").get("leaks", []))
        for row in rows)
    usage = list(conn.execute(
        "SELECT provider, status, tokens_in, tokens_out, est_cost_usd,"
        " notes FROM usage_log WHERE id > ?", (start,)))
    reported = sum(r["est_cost_usd"] for r in usage
                   if r["status"] in ("ok", "failed_known"))
    estimated = sum(r["est_cost_usd"] for r in usage
                    if r["status"] == "failed_unknown")
    meta_rows = [r for r in usage if r["provider"] == "meta"]
    meta_ok = [r for r in meta_rows if r["status"] == "ok"]
    empty = sum(1 for r in meta_rows if "empty reply" in (r["notes"] or ""))
    truncated = sum(1 for r in meta_rows if "truncated" in (r["notes"] or ""))
    summary = {
        "cases": len(rows),
        "passed": sum(1 for row in rows if row["ok"]),
        "leaks": leaks,
        "by_scenario": by_scenario,
        "groups": groups,
        "spend_usd": round(reported + estimated, 6),
        "spend_reported_usd": round(reported, 6),
        "spend_estimated_usd": round(estimated, 6),
        "ledger_note": ("Local ledger, this run only: estimates, not"
                        " the provider's authoritative billing record."),
        "tokens": sum(r["tokens_in"] + r["tokens_out"] for r in usage),
        "meta_provider_rows": len(meta_rows),
        "meta_success_rows": len(meta_ok),
        "cost_per_successful_meta_task_usd": (
            round(sum(r["est_cost_usd"] for r in meta_ok)
                  / len(meta_ok), 6) if meta_ok else 0.0),
        "empty_response_rate": (
            round(empty / len(meta_rows), 4) if meta_rows else 0.0),
        "truncated_response_rate": (
            round(truncated / len(meta_rows), 4) if meta_rows else 0.0),
        "fallback_rate": (
            round(fallbacks / provider_cases, 4)
            if provider_cases else 0.0),
    }
    if live:
        summary["requests"] = ctx.requests
        summary["errors"] = ctx.errors
        ordered = sorted(ctx.latencies)
        summary["avg_latency_ms"] = round(
            sum(ordered) / len(ordered), 1) if ordered else 0.0
        summary["p50_latency_ms"] = _percentile(ordered, 50)
        summary["p95_latency_ms"] = _percentile(ordered, 95)
        summary["reasoning_tokens_total"] = sum(
            int(match.group(1)) for row in usage
            for match in re.finditer(r"reasoning_tokens=(\d+)",
                                     row["notes"] or ""))
        # The vendor reports no cached-token field on this surface;
        # null means "not supplied", never zero.
        summary["cached_tokens_total"] = None
    else:
        planned = sum(
            json.loads(row["metrics_json"] or "{}").get(
                "planned_requests", 0) for row in rows)
        est = conn.execute(
            "SELECT COALESCE(SUM(est_cost_usd), 0) AS total FROM eval_cases"
            " WHERE run_id = ?", (run_id,)).fetchone()["total"]
        summary["planned_requests"] = planned
        summary["est_cost_usd_upper_bound"] = round(est, 6)
    return summary


def main(argv: list[str] | None = None) -> dict:
    """Run the harness. Returns the summary dict (also printed)."""
    args = build_parser().parse_args(argv)
    try:
        args.chat_max_tokens = check_output_cap(
            args.chat_max_tokens, default=DEFAULT_CHAT_MAX_TOKENS,
            ceiling=MAX_CHAT_TOKENS)
    except ValueError as exc:
        print(f"Evaluation refused: {exc}", file=sys.stderr)
        return {"status": "refused", "reason": str(exc)}
    live = bool(args.live)
    if live and os.environ.get("MIRA_EVAL_LIVE") != "1":
        print("Live mode refused: set MIRA_EVAL_LIVE=1 to opt in.",
              file=sys.stderr)
        return {"status": "refused", "reason": "live opt-in missing"}
    try:
        selected = _selected_scenarios(args)
    except EvalRefused as exc:
        print(f"Evaluation refused: {exc}", file=sys.stderr)
        return {"status": "refused", "reason": str(exc)}
    if live:
        try:
            _check_live_preconditions(args)
            # Pre-run spending gate: the planned worst case must fit
            # the cap before anything is created or sent.
            planned = _planned_requests(args, selected)
            worst = planned * _worst_per_request(settings.meta_model)
            if worst > args.max_cost_usd:
                raise EvalRefused(
                    f"planned worst-case ${worst:.2f} for {planned}"
                    f" requests exceeds --max-cost-usd"
                    f" ${args.max_cost_usd:.2f}")
        except EvalRefused as exc:
            print(f"Live mode refused: {exc}", file=sys.stderr)
            return {"status": "refused", "reason": str(exc)}
    try:
        conn = _init_eval_db(args.db_path)
    except EvalRefused as exc:
        print(f"Evaluation refused: {exc}", file=sys.stderr)
        return {"status": "refused", "reason": str(exc)}
    try:
        cur = conn.execute(
            "INSERT INTO eval_runs (started_at, mode, seed, config_json)"
            " VALUES (?, ?, ?, ?)",
            (_stamp(), "live" if live else "dry-run", args.seed,
             json.dumps({"scenarios": args.scenarios,
                         "cases": args.cases, "turns": args.turns,
                         "chat_max_tokens": args.chat_max_tokens,
                         "max_requests": args.max_requests,
                         "max_cost_usd": args.max_cost_usd,
                         "max_errors": args.max_errors,
                         "max_tokens": args.max_tokens,
                         "max_call_cost_usd":
                         args.max_call_cost_usd})))
        run_id = cur.lastrowid
        conn.commit()
        ctx = Ctx(conn, args, run_id)
        status, stop_reason = "complete", ""
        try:
            total = len(selected) * max(1, args.cases)
            twins = synth.synthetic_twins(2 * total + 2, args.seed,
                                          tag=f".r{run_id}")
            jobs = synth.synthetic_jobs(total + 1, args.seed + 1)
            needs = synth.synthetic_needs(total + 1, args.seed + 2)
            case_no = 0
            for scenario in selected:
                for idx in range(args.cases):
                    try:
                        result = _dispatch(ctx, scenario, idx, case_no,
                                           twins, jobs, needs)
                    except _Stop as exc:
                        status, stop_reason = "stopped", str(exc)
                        break
                    case_no += 1
                    est = (result.metrics.get("est_cost_usd", 0.0)
                           if not live else 0.0)
                    ctx.record(result, idx, est_cost=est)
                if status == "stopped":
                    break
            for offline in S.OFFLINE_SCENARIOS:
                for err in offline():
                    ctx.record(err, 0)
        except _Stop as exc:
            status, stop_reason = "stopped", str(exc)
        except EvalRefused as exc:
            status, stop_reason = "refused", str(exc)
        summary = _summarize(conn, run_id, ctx)
        summary.update({"status": status, "run_id": run_id,
                        "mode": "live" if live else "dry-run"})
        if stop_reason:
            summary["stop_reason"] = stop_reason
        conn.execute("UPDATE eval_runs SET status = ?, stop_reason = ?"
                     " WHERE id = ?", (status, stop_reason, run_id))
        conn.commit()
        print(json.dumps(summary, indent=2))
        return summary
    finally:
        conn.close()


if __name__ == "__main__":
    main()
