"""Mock adapter for the unified interface ($0, always available).

Clearly labelled mock: every result has mock=True and zero tokens.
"""

from __future__ import annotations

from .base import BaseAdapter
from ..types import AIResult, Capabilities


class MockAdapter(BaseAdapter):
    name = "mock"
    capabilities = Capabilities(text=True, structured_output=True)

    def __init__(self, model: str = "mock", api_key: str = "",
                 post=None):
        super().__init__(model=model or "mock", api_key="", post=post)

    def available(self) -> bool:
        return True

    def generate_text(self, messages: list) -> AIResult:
        return AIResult(text="Mock reply (no provider configured).",
                        provider="mock", model=self.model, mock=True)

    def generate_structured(self, messages: list,
                            json_schema: dict) -> AIResult:
        return AIResult(text="{}", provider="mock", model=self.model,
                        structured={}, mock=True)
