"""Reply-provider interface and router.

The conversation engine never calls a model directly; it asks the configured
provider for the next twin message. Default is the deterministic MockProvider
($0). The Meta provider activates only when AI_PROVIDER=meta is set AND a
verified Standard-mode model and key are configured AND budgets allow --
otherwise every call path refuses or falls back to mock with a visible notice.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import mock_mind
from .config import settings
from .log import get_logger
from .meta_client import MetaProvider


@dataclass(frozen=True)
class ProviderResult:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0


class MockProvider:
    name = "mock"

    def generate(self, context) -> ProviderResult:  # context: conversations.TwinContext
        return ProviderResult(text=mock_mind.generate(context))


def get_provider(name: str | None = None):
    which = (name or settings.ai_provider or "mock").lower()
    if which == "meta":
        return MetaProvider()
    if which != "mock":
        get_logger().warning(
            "unknown provider %r; falling back to mock", which)
    return MockProvider()
