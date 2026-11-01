"""Multi-provider AI orchestration (Phase 1).

One registry, one router, one adapter per vendor. Legacy chat/intent
paths keep working untouched; new agent code calls `run_task`.
"""

from .adapters import (
    AnthropicAdapter,
    BaseAdapter,
    GeminiAdapter,
    MetaAdapter,
    MockAdapter,
    OpenAIAdapter,
)
from .registry import REGISTRY, ProviderRegistry, ProviderSpec, default_registry
from .retry import call_with_retry
from .router import new_project, new_task, parse_routes, route, run_task
from .telemetry import record_ai_usage, timed
from .types import (
    AIResult,
    Capabilities,
    ProjectContext,
    ProviderError,
    ProviderNotConfigured,
    TaskContext,
    TaskKind,
    ToolCall,
    UnsupportedOperation,
)

__all__ = [
    "AIResult",
    "AnthropicAdapter",
    "BaseAdapter",
    "Capabilities",
    "GeminiAdapter",
    "MetaAdapter",
    "MockAdapter",
    "OpenAIAdapter",
    "ProjectContext",
    "ProviderError",
    "ProviderNotConfigured",
    "ProviderRegistry",
    "ProviderSpec",
    "REGISTRY",
    "TaskContext",
    "TaskKind",
    "ToolCall",
    "UnsupportedOperation",
    "call_with_retry",
    "default_registry",
    "new_project",
    "new_task",
    "parse_routes",
    "record_ai_usage",
    "route",
    "run_task",
    "timed",
]
