"""Task routing + the budgeted, retried, telemetered call path.

`route()` picks a configured adapter for a task kind (AI_ROUTES map,
then the global default, then mock). `run_task()` executes one agent
task end to end: budgets first, then retry + latency capture, then
usage accounting with full provenance.
"""

from __future__ import annotations

import json
from contextlib import nullcontext
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


#: Enforceable client-side request-shape limits for run_task.
#: Checked before routing, budgeting, and any network attempt; oversized
#: input raises ValueError (no usage row: nothing was sent). Sized so a
#: single text/structured call stays near legacy chat bounds (~4 chars
#: per token: 60k chars ~= 15k input tokens); tool loops additionally
#: re-send history per round server-side, which no client constant can
#: bound exactly -- the unknown-usage estimate stays the backstop there.
MAX_ROUTER_MESSAGES = 50
MAX_ROUTER_MESSAGE_CHARS = 12_000
MAX_ROUTER_TOTAL_CHARS = 60_000
MAX_ROUTER_TOOLS = 20
MAX_ROUTER_TOOL_DEFINITIONS_CHARS = 20_000
MAX_ROUTER_SCHEMA_CHARS = 20_000


def _sized_text(value: Any) -> int:
    if isinstance(value, dict):
        value = value.get("content", "")
    if not isinstance(value, str):
        try:
            value = json.dumps(value, default=str)
        except (TypeError, ValueError):
            value = str(value)
    return len(value)


def _check_request_shape(messages: list, tools: list | None,
                         structured_schema: dict | None) -> None:
    """Reject oversized caller input before any provider request."""
    messages = messages or []
    if len(messages) > MAX_ROUTER_MESSAGES:
        raise ValueError(
            f"Too many messages ({len(messages)}): the router accepts at"
            f" most {MAX_ROUTER_MESSAGES}.")
    total = 0
    for message in messages:
        size = _sized_text(message)
        if size > MAX_ROUTER_MESSAGE_CHARS:
            raise ValueError(
                f"Message too large ({size} chars): the router accepts"
                f" at most {MAX_ROUTER_MESSAGE_CHARS} chars per message.")
        total += size
    if total > MAX_ROUTER_TOTAL_CHARS:
        raise ValueError(
            f"Messages too large ({total} chars total): the router"
            f" accepts at most {MAX_ROUTER_TOTAL_CHARS} chars total.")
    if tools:
        if len(tools) > MAX_ROUTER_TOOLS:
            raise ValueError(
                f"Too many tools ({len(tools)}): the router accepts at"
                f" most {MAX_ROUTER_TOOLS}.")
        defs_size = sum(
            len(json.dumps(t, default=str)) for t in tools)
        if defs_size > MAX_ROUTER_TOOL_DEFINITIONS_CHARS:
            raise ValueError(
                f"Tool definitions too large ({defs_size} chars): the"
                f" router accepts at most"
                f" {MAX_ROUTER_TOOL_DEFINITIONS_CHARS} chars total.")
    if structured_schema is not None:
        schema_size = len(json.dumps(structured_schema, default=str))
        if schema_size > MAX_ROUTER_SCHEMA_CHARS:
            raise ValueError(
                f"Schema too large ({schema_size} chars): the router"
                f" accepts at most {MAX_ROUTER_SCHEMA_CHARS} chars.")


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

    An explicit request naming a known provider is strict: when that
    provider is unavailable or lacks the capability, this raises
    instead of substituting another provider, so a result labelled
    with a real provider always came from that provider. Unknown
    explicit names keep the historical warn-and-fall-through path.
    """
    if explicit and explicit.strip().lower() in REGISTRY.names:
        wanted = explicit.strip().lower()
        adapter = REGISTRY.get(wanted)
        if capability is not None and not adapter.capabilities.supports(
                capability):
            raise ProviderNotConfigured(
                f"Explicit provider {wanted!r} does not support"
                f" capability={capability!r}.")
        if not adapter.available():
            raise ProviderNotConfigured(
                f"Explicit provider {wanted!r} is not available"
                " (model/key missing or invalid); refusing to"
                " substitute another provider.")
        return adapter
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


def _diagnostic_notes(result: AIResult) -> str:
    """Ledger note for vendor diagnostics (finish/reasoning).

    Present only when the vendor reported the fields; never invented.
    Lets cost analysis separate visible output from reasoning spend.
    """
    parts = []
    if result.finish_reason:
        parts.append(f"finish_reason={result.finish_reason}")
    if result.reasoning_tokens is not None:
        parts.append(f"reasoning_tokens={result.reasoning_tokens}")
    return "; ".join(parts)


def _record_provider_failure(conn, *, adapter, conv_id, user_id,
                           exc: ProviderError) -> None:
    """Leave one usage row for a failed provider attempt.

    Known usage keeps its real cost (failed_known); otherwise the row
    is failed_unknown priced at the conservative estimate, so budget
    checks count the possibly-billed attempt instead of ignoring it.
    """
    if exc.usage_known:
        record_ai_usage(
            conn, provider=adapter.name, model=adapter.model,
            conv_id=conv_id, user_id=user_id,
            tokens_in=exc.tokens_in or 0,
            tokens_out=exc.tokens_out or 0,
            status="failed_known", notes=str(exc)[:300])
        if exc.uncertain_extra:
            # Known partial usage plus one more uncertain attempt
            # (e.g. a tool loop that billed N rounds, then a round
            # with no trustworthy usage): the estimate covers the
            # extra attempt, never the known part twice.
            record_ai_usage(
                conn, provider=adapter.name, model=adapter.model,
                conv_id=conv_id, user_id=user_id,
                status="failed_unknown",
                notes=f"Uncertain extra attempt after known partial"
                      f" usage ({exc.tokens_in}+{exc.tokens_out} tok).")
    else:
        record_ai_usage(
            conn, provider=adapter.name, model=adapter.model,
            conv_id=conv_id, user_id=user_id,
            status="failed_unknown", notes=str(exc)[:300])


def run_task(conn, task: TaskContext, *, messages: list,
             conv_id=None, user_id=None,
             structured_schema: dict | None = None,
             tools: list | None = None,
             tool_handler: Callable | None = None,
             provider_name: str | None = None,
             fallback_to_mock: bool = True,
             max_attempts: int = 3,
             max_tokens: int | None = None) -> AIResult:
    """Execute one agent task with budgets, retry, and telemetry.

    Budgets are checked before every network attempt (retries included).
    Each failed attempt leaves failed_known/failed_unknown usage rows:
    one row per attempt, plus one extra unknown row when an attempt has
    both known partial usage and an additional uncertain round.
    With fallback_to_mock, a failed real call degrades to a clearly
    labelled mock result instead of raising. Budget exhaustion is a
    hard refusal and never falls back. Oversized caller input is
    rejected with ValueError before routing or budgeting. An explicit
    provider_name is strict: an unavailable named provider raises
    ProviderNotConfigured instead of silently running on mock. The
    max_tokens override applies to Meta text calls only (1-1500,
    default 1500); any other combination raises ValueError.
    """
    _check_request_shape(messages, tools, structured_schema)
    capability = ("tool_calls" if tools else
                  ("structured_output" if structured_schema is not None
                   else "text"))
    adapter = route(task.kind, capability=capability,
                    explicit=provider_name)
    if max_tokens is not None and (
            tools or structured_schema is not None
            or adapter.name != "meta"):
        raise ValueError(
            "max_tokens override applies to Meta text calls only.")

    def attempt() -> AIResult:
        # Re-checked per attempt so an exhausted budget stops retries.
        if adapter.name != "mock":
            budget_engine.check_budgets(conn, user_id, conv_id)
        if tools:
            assert tool_handler is not None, "tools need a handler"
            return adapter.run_tools(messages, tools, tool_handler)
        if structured_schema is not None:
            return adapter.generate_structured(messages,
                                               structured_schema)
        if max_tokens is not None:
            return adapter.generate_text(messages,
                                         max_tokens=max_tokens)
        return adapter.generate_text(messages)

    def note_retry(exc: ProviderError, attempt_no: int) -> None:
        _record_provider_failure(conn, adapter=adapter, conv_id=conv_id,
                                 user_id=user_id, exc=exc)

    guard = (budget_engine.paid_call_guard()
             if adapter.name != "mock" else nullcontext())
    with guard:
        if adapter.name != "mock":
            # Refuse unpriced vendors before any network attempt.
            budget_engine.estimate_cost_for(adapter.name, adapter.model, 0, 0)
        try:
            (result, attempts_used), latency_ms = timed(
                lambda: call_with_retry(attempt, attempts=max_attempts,
                                        on_retry=note_retry))
            result = AIResult(
                text=result.text, provider=result.provider,
                model=result.model, tokens_in=result.tokens_in,
                tokens_out=result.tokens_out, latency_ms=latency_ms,
                attempts=attempts_used, structured=result.structured,
                tool_calls=result.tool_calls, mock=result.mock,
                finish_reason=result.finish_reason,
                reasoning_tokens=result.reasoning_tokens)
            try:
                cost = record_ai_usage(
                    conn, provider=result.provider, model=result.model,
                    conv_id=conv_id, user_id=user_id,
                    tokens_in=result.tokens_in,
                    tokens_out=result.tokens_out,
                    latency_ms=result.latency_ms,
                    attempts=result.attempts,
                    notes=_diagnostic_notes(result))
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
                       "attempts": result.attempts,
                       "cost_usd": round(cost, 6),
                       "mock": result.mock})
            task.project.note(
                f"{task.task_id}: {result.provider}/{result.model}"
                f" ({result.tokens_in}+{result.tokens_out} tok,"
                f" {result.latency_ms}ms)") if task.project else None
            return result
        except ProviderError as exc:
            _record_provider_failure(conn, adapter=adapter,
                                     conv_id=conv_id, user_id=user_id,
                                     exc=exc)
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
