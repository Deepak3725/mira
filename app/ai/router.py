"""Task routing + the budgeted, retried, telemetered call path.

`route()` picks a configured adapter for a task kind (AI_ROUTES map,
then the global default, then mock). `run_task()` executes one agent
task end to end: budgets first, then retry + latency capture, then
usage accounting with full provenance.
"""

from __future__ import annotations

from typing import Any, Callable

from .. import budget as budget_engine
from ..config import settings
from ..log import get_logger
from .adapters import BaseAdapter
from .registry import REGISTRY
from .retry import call_with_retry
from .telemetry import record_ai_usage, timed
from .types import (
    AIResult,
    ProjectContext,
    ProviderError,
    ProviderNotConfigured,
    TaskContext,
)


def parse_routes(raw: str) -> dict[str, str]:
    """Parse 'kind:provider,...' into a map. Malformed pairs dropped."""
    routes: dict[str, str] = {}
    for chunk in (raw or "").split(","):
        if ":" not in chunk:
            continue
        kind, _, name = chunk.partition(":")
        kind, name = kind.strip().lower(), name.strip().lower()
        if kind and name:
            routes[kind] = name
    return routes


def route(task_kind: str, *, capability: str | None = None,
          explicit: str | None = None,
          allow_mock: bool = True) -> BaseAdapter:
    """Select an available adapter for a task kind.

    Order: explicit name, AI_ROUTES entry, global AI_PROVIDER default,
    mock fallback. Candidates lacking a required capability are
    skipped. Raises ProviderNotConfigured when nothing qualifies.
    """
    routes = parse_routes(settings.ai_routes)
    candidates = [explicit, routes.get((task_kind or "").lower()),
                  settings.ai_provider or "mock"]
    tried: list[str] = []
    for name in candidates:
        if not name:
            continue
        adapter = REGISTRY.get(name)
        tried.append(adapter.name)
        if capability is not None and not adapter.capabilities.supports(
                capability):
            continue
        if adapter.available():
            return adapter
    if allow_mock and (capability is None
                       or REGISTRY.get("mock").capabilities.supports(
                           capability)):
        return REGISTRY.get("mock")
    raise ProviderNotConfigured(
        f"No available provider for kind={task_kind!r}"
        f" capability={capability!r} (tried: {', '.join(tried)}).")


def run_task(conn, task: TaskContext, *, messages: list,
             conv_id=None, user_id=None,
             structured_schema: dict | None = None,
             tools: list | None = None,
             tool_handler: Callable | None = None,
             provider_name: str | None = None,
             fallback_to_mock: bool = True,
             max_attempts: int = 3) -> AIResult:
    """Execute one agent task with budgets, retry, and telemetry.

    Budgets are checked before any network attempt. Failures leave
    failed_known/failed_unknown usage rows (mirroring the chat path);
    with fallback_to_mock, a failed real call degrades to a clearly
    labelled mock result instead of raising.
    """
    capability = ("tool_calls" if tools else
                  ("structured_output" if structured_schema is not None
                   else "text"))
    adapter = route(task.kind, capability=capability,
                    explicit=provider_name)
    if adapter.name != "mock":
        budget_engine.check_budgets(conn, user_id, conv_id)
        # Refuse unpriced vendors before any network attempt.
        budget_engine.estimate_cost_for(adapter.name, adapter.model, 0, 0)

    def attempt() -> AIResult:
        if tools:
            assert tool_handler is not None, "tools need a handler"
            return adapter.run_tools(messages, tools, tool_handler)
        if structured_schema is not None:
            return adapter.generate_structured(messages,
                                               structured_schema)
        return adapter.generate_text(messages)

    try:
        (result, attempts_used), latency_ms = timed(
            lambda: call_with_retry(attempt, attempts=max_attempts))
        result = AIResult(
            text=result.text, provider=result.provider,
            model=result.model, tokens_in=result.tokens_in,
            tokens_out=result.tokens_out, latency_ms=latency_ms,
            attempts=attempts_used, structured=result.structured,
            tool_calls=result.tool_calls, mock=result.mock)
        try:
            cost = record_ai_usage(
                conn, provider=result.provider, model=result.model,
                conv_id=conv_id, user_id=user_id,
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                latency_ms=result.latency_ms,
                attempts=result.attempts)
        except budget_engine.BudgetExhausted:
            # Unpriced vendor (see VENDOR_PRICES): the call ran, so it
            # must still leave a row -- with unknown cost, never $0.
            record_ai_usage(
                conn, provider=result.provider, model=result.model,
                conv_id=conv_id, user_id=user_id,
                tokens_in=0, tokens_out=0,
                latency_ms=result.latency_ms,
                attempts=result.attempts, status="failed_unknown",
                notes=f"No verified pricing for {result.provider}/"
                      f"{result.model}; verify vendor dashboard.")
            raise
        get_logger().info(
            "ai call",
            extra={"provider": result.provider, "model": result.model,
                   "kind": task.kind, "tokens_in": result.tokens_in,
                   "tokens_out": result.tokens_out,
                   "latency_ms": result.latency_ms,
                   "attempts": result.attempts, "cost_usd": round(cost, 6),
                   "mock": result.mock})
        task.project.note(
            f"{task.task_id}: {result.provider}/{result.model}"
            f" ({result.tokens_in}+{result.tokens_out} tok,"
            f" {result.latency_ms}ms)") if task.project else None
        return result
    except ProviderError as exc:
        if exc.usage_known:
            record_ai_usage(
                conn, provider=adapter.name, model=adapter.model,
                conv_id=conv_id, user_id=user_id,
                tokens_in=exc.tokens_in or 0,
                tokens_out=exc.tokens_out or 0,
                status="failed_known", notes=str(exc)[:300])
        else:
            record_ai_usage(
                conn, provider=adapter.name, model=adapter.model,
                conv_id=conv_id, user_id=user_id,
                status="failed_unknown", notes=str(exc)[:300])
        if fallback_to_mock and adapter.name != "mock":
            get_logger().warning("ai fallback to mock for %s: %s",
                                 adapter.name, exc)
            mock_result = REGISTRY.get("mock").generate_text(messages)
            record_ai_usage(
                conn, provider="mock", model=mock_result.model,
                conv_id=conv_id, user_id=user_id)
            return mock_result
        raise


def new_project(project_id: str, goal: str) -> ProjectContext:
    """Start a shared context that agents pass results through."""
    return ProjectContext(project_id=project_id, goal=goal)


def new_task(project: ProjectContext, task_id: str, kind: str,
             instructions: str, **inputs: Any) -> TaskContext:
    """Start one agent task inside a shared project context."""
    return TaskContext(task_id=task_id, kind=kind,
                       instructions=instructions,
                       inputs=dict(inputs), project=project)
