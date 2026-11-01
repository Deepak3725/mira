"""Shared types for the multi-provider AI layer.

Every provider result carries its provenance (provider/model/mock flag)
so callers can always tell real execution from mock execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ProviderNotConfigured(Exception):
    """The provider cannot run (disabled, or model/key missing)."""


class UnsupportedOperation(Exception):
    """The adapter does not support this operation (honest refusal)."""


class ProviderError(Exception):
    """A provider call failed (network, auth, rate limit, bad response).

    ``retryable`` marks failures worth retrying (429, 5xx, timeouts).
    Token usage, when the vendor reported it, is preserved so failed
    calls are still accounted; None means unknown, never zero.
    """

    def __init__(self, message: str, *, retryable: bool = False,
                 tokens_in: int | None = None,
                 tokens_out: int | None = None,
                 retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.retry_after = retry_after

    @property
    def usage_known(self) -> bool:
        return self.tokens_in is not None and self.tokens_out is not None


@dataclass(frozen=True)
class Capabilities:
    """What an adapter can actually do. False = honestly unsupported."""

    text: bool = True
    structured_output: bool = False
    tool_calls: bool = False
    images_in: bool = False
    images_out: bool = False
    audio_in: bool = False
    audio_out: bool = False

    def supports(self, capability: str) -> bool:
        return bool(getattr(self, capability, False))


@dataclass(frozen=True)
class ToolCall:
    """One model-requested tool invocation."""

    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""


@dataclass(frozen=True)
class AIResult:
    """One completed provider call, with full provenance."""

    text: str
    provider: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    attempts: int = 1
    structured: dict[str, Any] | None = None
    tool_calls: list[ToolCall] | None = None
    mock: bool = False


class TaskKind:
    """Task types used for model routing (plain strings)."""

    CHAT = "chat"
    INTENT = "intent"
    WRITING = "writing"
    CODE = "code"
    RESEARCH = "research"
    ANALYSIS = "analysis"
    TEST = "test"
    REVIEW = "review"
    IMAGE = "image"


@dataclass
class TaskContext:
    """Everything one agent task needs, passed between agents."""

    task_id: str
    kind: str
    instructions: str
    inputs: dict[str, Any] = field(default_factory=dict)
    prior_outputs: list[AIResult] = field(default_factory=list)
    project: ProjectContext | None = None

    def with_output(self, result: AIResult) -> TaskContext:
        """Return a copy carrying one more prior output."""
        return TaskContext(
            task_id=self.task_id, kind=self.kind,
            instructions=self.instructions,
            inputs=dict(self.inputs),
            prior_outputs=[*self.prior_outputs, result],
            project=self.project,
        )


@dataclass
class ProjectContext:
    """Shared context all agents on a project can read and extend."""

    project_id: str
    goal: str
    notes: list[str] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
    history: list[str] = field(default_factory=list)

    def note(self, entry: str) -> None:
        self.history.append(entry)
