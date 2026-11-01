"""Shared types for universal knowledge import."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Candidate:
    """One reviewable fact extracted from an export."""

    content: str
    source: str
    category: str = "general"
    source_time: str | None = None
    attachments: tuple[str, ...] = ()


@dataclass
class ImportResult:
    """Everything one import produced (never auto-approved)."""

    provider: str
    candidates: list[Candidate] = field(default_factory=list)
    skipped: int = 0
    attachments: int = 0
    warnings: list[str] = field(default_factory=list)
