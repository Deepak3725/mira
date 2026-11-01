"""Vendor adapters (one module per provider)."""

from .anthropic import AnthropicAdapter
from .base import BaseAdapter, http_post_json
from .gemini import GeminiAdapter
from .meta import MetaAdapter
from .mock import MockAdapter
from .openai import OpenAIAdapter

__all__ = [
    "AnthropicAdapter",
    "BaseAdapter",
    "GeminiAdapter",
    "MetaAdapter",
    "MockAdapter",
    "OpenAIAdapter",
    "http_post_json",
]
