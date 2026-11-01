"""Voice provider registry. Unknown names fall back to browser."""

from __future__ import annotations

from .providers import (
    BrowserVoiceProvider,
    MetaTranscribeProvider,
    MockVoiceProvider,
    TextFallbackProvider,
)

_PROVIDERS = {
    "browser": BrowserVoiceProvider,
    "text": TextFallbackProvider,
    "mock": MockVoiceProvider,
    "meta": MetaTranscribeProvider,
}

DEFAULT_PROVIDER = "browser"


def available() -> tuple[str, ...]:
    return tuple(_PROVIDERS)


def default_provider():
    return _PROVIDERS[DEFAULT_PROVIDER]()


def get_provider(name: str):
    """Resolve a provider name. Blank/unknown → browser default."""
    cls = _PROVIDERS.get((name or "").strip().lower())
    if cls is None:
        cls = _PROVIDERS[DEFAULT_PROVIDER]
    return cls()
