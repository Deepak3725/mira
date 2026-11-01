"""Agent assignment: pick a provider/model per task kind.

Task kinds (writing, code, research, analysis, test, review, image)
route through the Phase 1 router, so AI_ROUTES and availability rule:
examples like "code:openai" are configuration, never hardcoded
assignments. Image tasks need images_out capability, which no
adapter has -- they refuse honestly instead of faking pictures.
"""

from __future__ import annotations

from ..ai import route
from ..ai.types import ProviderNotConfigured
from .graph import ProjectError

#: Kind -> required adapter capability (None = plain text suffices).
KIND_CAPABILITY: dict[str, str | None] = {
    "chat": None,
    "writing": None,
    "code": None,
    "research": None,
    "analysis": None,
    "test": None,
    "review": None,
    "intent": "structured_output",
    "image": "images_out",
}


def assign(task_kind: str, *, explicit: str | None = None,
           allow_mock: bool = True) -> tuple[str, str]:
    """Return (provider_name, model) for a task kind.

    Raises ProjectError when nothing qualifies (message-safe for UI).
    """
    capability = KIND_CAPABILITY.get((task_kind or "").lower())
    if task_kind and task_kind.lower() not in KIND_CAPABILITY:
        capability = None
    try:
        adapter = route(task_kind, capability=capability,
                        explicit=explicit, allow_mock=allow_mock)
    except ProviderNotConfigured as exc:
        raise ProjectError(str(exc)) from exc
    return adapter.name, adapter.model
