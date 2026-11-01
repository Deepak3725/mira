"""Provider registry: one factory per vendor, independent enablement.

Each vendor is enabled only by its own configuration (model + key);
missing pieces mean the entry reports unavailable and the router
skips it. Unknown names fall back to mock with a visible warning.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..config import settings
from ..log import get_logger
from .adapters import (
    AnthropicAdapter,
    BaseAdapter,
    GeminiAdapter,
    MetaAdapter,
    MockAdapter,
    OpenAIAdapter,
)


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    factory: Callable[[], BaseAdapter]
    description: str = ""


class ProviderRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ProviderSpec] = {}

    def register(self, spec: ProviderSpec) -> None:
        self._specs[spec.name] = spec

    @property
    def names(self) -> list[str]:
        return sorted(self._specs)

    def get(self, name: str | None = None) -> BaseAdapter:
        which = (name or settings.ai_provider or "mock").lower()
        spec = self._specs.get(which)
        if spec is None:
            get_logger().warning(
                "unknown provider %r; falling back to mock", which)
            return self._specs["mock"].factory()
        return spec.factory()

    def available(self) -> dict[str, bool]:
        return {name: spec.factory().available()
                for name, spec in self._specs.items()}


def default_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(ProviderSpec(
        "mock", MockAdapter, "Deterministic $0 mock (always available)."))
    registry.register(ProviderSpec(
        "meta",
        lambda: MetaAdapter(model=settings.meta_model,
                            api_key=settings.model_api_key),
        "Meta Muse Spark (Model API key + priced Standard model)."))
    registry.register(ProviderSpec(
        "openai",
        lambda: OpenAIAdapter(model=settings.openai_model,
                              api_key=settings.openai_api_key),
        "OpenAI API (API key + explicit model; consumer plans excluded)."))
    registry.register(ProviderSpec(
        "gemini",
        lambda: GeminiAdapter(model=settings.gemini_model,
                              api_key=settings.gemini_api_key),
        "Google Gemini API (API key + explicit model)."))
    registry.register(ProviderSpec(
        "anthropic",
        lambda: AnthropicAdapter(model=settings.anthropic_model,
                                 api_key=settings.anthropic_api_key),
        "Anthropic Claude API (API key + explicit model)."))
    return registry


REGISTRY = default_registry()
