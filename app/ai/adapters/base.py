"""Adapter interface. Every vendor implements this surface honestly:
unsupported operations raise UnsupportedOperation, never fake results."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from ..types import (
    AIResult,
    Capabilities,
    ProviderError,
    ProviderNotConfigured,
    ToolCall,
    UnsupportedOperation,
)

TIMEOUT_SECONDS = 30


def http_post_json(url: str, payload: dict, headers: dict,
                   timeout: int = TIMEOUT_SECONDS) -> dict:
    """POST JSON and parse the JSON reply. Maps failures to ProviderError."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except OSError:
            detail = ""
        retryable = exc.code == 429 or 500 <= exc.code < 600
        retry_after = None
        if exc.headers is not None:
            raw = exc.headers.get("Retry-After")
            try:
                retry_after = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                retry_after = None
        raise ProviderError(
            f"HTTP {exc.code}: {detail}".rstrip(),
            retryable=retryable, retry_after=retry_after) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderError(f"Request failed: {exc}.",
                            retryable=True) from exc
    except ValueError as exc:
        raise ProviderError(
            f"Response was not valid JSON: {exc}.") from exc


def strict_usage(data, usage_key: str, in_key: str, out_key: str,
               vendor: str) -> tuple[int, int]:
    """Parse one vendor usage block strictly.

    Returns (tokens_in, tokens_out). A missing, null, malformed,
    boolean, or negative usage block raises ProviderError with
    unknown tokens (None), so the caller accounts the call through
    the failed_unknown estimate instead of understating spend as
    $0. Explicitly reported zeros are honored as known (0, 0).
    Mirrors the Meta client's usage validation.
    """
    usage = data.get(usage_key) if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        raise ProviderError(f"{vendor} API omitted usage accounting.")
    tokens_in, tokens_out = usage.get(in_key), usage.get(out_key)
    if isinstance(tokens_in, bool) or isinstance(tokens_out, bool):
        raise ProviderError(
            f"{vendor} API returned invalid usage accounting.")
    try:
        tokens_in, tokens_out = int(tokens_in), int(tokens_out)
    except (TypeError, ValueError) as exc:
        raise ProviderError(
            f"{vendor} API omitted usage accounting.") from exc
    if tokens_in < 0 or tokens_out < 0:
        raise ProviderError(
            f"{vendor} API returned invalid usage accounting.")
    return tokens_in, tokens_out


class BaseAdapter:
    """One AI vendor behind the unified interface."""

    name = "base"
    capabilities = Capabilities()

    def __init__(self, model: str = "", api_key: str = "",
                 post=None):
        self.model = model
        self.api_key = api_key
        # Injecteable transport: post(url, payload, headers) -> dict.
        self._post = post or http_post_json

    def available(self) -> bool:
        """True when this adapter is configured and could run."""
        return False

    def _require_ready(self) -> None:
        if not self.available():
            raise ProviderNotConfigured(
                f"{self.name} is not configured (model/key missing).")

    def generate_text(self, messages: list) -> AIResult:
        """One text reply for chat-style messages."""
        raise UnsupportedOperation(
            f"{self.name} does not support text generation.")

    def generate_structured(self, messages: list,
                            json_schema: dict) -> AIResult:
        """One reply parsed as a JSON object (schema-guided)."""
        raise UnsupportedOperation(
            f"{self.name} does not support structured output.")

    def run_tools(self, messages: list, tools: list,
                  handler) -> AIResult:
        """Agentic tool loop. ``handler(name, args)`` executes one call
        and returns a JSON-serializable result."""
        raise UnsupportedOperation(
            f"{self.name} does not support tool calling.")
