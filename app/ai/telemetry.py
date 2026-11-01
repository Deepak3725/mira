"""Latency capture and usage recording for provider calls."""

from __future__ import annotations

import time
from typing import Callable, TypeVar

from .. import budget as budget_engine

T = TypeVar("T")


def timed(call: Callable[[], T]) -> tuple[T, int]:
    """Run ``call`` and return (result, latency_ms)."""
    start = time.monotonic()
    result = call()
    return result, int((time.monotonic() - start) * 1000)


def record_ai_usage(conn, *, provider: str, model: str,
                    conv_id=None, user_id=None,
                    tokens_in: int = 0, tokens_out: int = 0,
                    latency_ms: int = 0, attempts: int = 1,
                    status: str = "ok", notes: str = "") -> float:
    """Record one provider call with full telemetry. Returns est. cost."""
    return budget_engine.record_usage(
        conn, provider=provider, model=model, conv_id=conv_id,
        user_id=user_id, tokens_in=tokens_in, tokens_out=tokens_out,
        status=status, notes=notes, latency_ms=latency_ms,
        attempts=attempts)
