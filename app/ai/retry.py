"""Retry with exponential backoff for provider calls."""

from __future__ import annotations

import time
from typing import Callable, TypeVar

from .types import ProviderError

T = TypeVar("T")


def call_with_retry(call: Callable[[], T], *, attempts: int = 3,
                    base_delay: float = 1.0, max_delay: float = 30.0,
                    sleeper: Callable[[float], None] | None = None,
                    on_retry: Callable[[ProviderError, int], None] | None = None,
                    ) -> tuple[T, int]:
    """Run ``call``; retry retryable ProviderErrors with backoff.

    Returns (result, attempts_used). Non-retryable errors and any other
    exception propagate immediately. A server Retry-After hint wins over
    the computed backoff. ``sleeper`` is injectable so tests never sleep.
    ``on_retry(exc, attempt)`` fires for each failure that WILL be
    retried (never for the terminal failure), so callers can account
    each possibly-billed attempt exactly once.
    """
    sleep = sleeper or time.sleep
    last: ProviderError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return call(), attempt
        except ProviderError as exc:
            last = exc
            if not exc.retryable or attempt >= attempts:
                raise
            if on_retry is not None:
                on_retry(exc, attempt)
            delay = base_delay * (2 ** (attempt - 1))
            if exc.retry_after is not None:
                delay = max(0.0, float(exc.retry_after))
            sleep(min(delay, max_delay))
    raise last  # pragma: no cover - loop always returns or raises
