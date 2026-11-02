"""Offline-first evaluation harness for MIRA's AI paths (Phase C).

Measures useful outcomes on synthetic data only: reply validity,
match-explanation consistency, privacy leakage (zero tolerance),
prompt-injection resistance, and error handling -- plus usage,
latency, error rate, and cost summaries from the real ledger.

Two modes, never mixed up:
  dry-run (default): builds every prompt, estimates tokens/cost from
    prompt sizes and caps. Makes NO provider calls.
  live: requires BOTH the --live flag AND MIRA_EVAL_LIVE=1. Real
    Meta calls with per-batch caps and stop conditions. Results go
    to a separate eval database, never the app database.
"""

from __future__ import annotations

__all__ = ["main"]


def __getattr__(name: str):
    # Lazy so `python -m app.eval.runner` does not double-import.
    if name == "main":
        from .runner import main

        return main
    raise AttributeError(f"app.eval has no attribute {name!r}")
