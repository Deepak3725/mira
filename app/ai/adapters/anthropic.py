"""Anthropic adapter (Messages wire, stdlib urllib).

Wire notes follow Anthropic's public Messages API (x-api-key auth,
system/messages/max_tokens, content blocks, input/output_tokens).
No Anthropic key is configured in this project, so live calls are NOT
yet verified: the suite exercises this adapter only through injected
fake transports. Consumer Claude subscriptions do not grant API access.
"""

from __future__ import annotations

import json

from ..types import (
    AIResult,
    Capabilities,
    ProviderError,
    ProviderNotConfigured,
    ToolCall,
)
from .base import BaseAdapter

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
MAX_TOOL_ROUNDS = 5


class AnthropicAdapter(BaseAdapter):
    name = "anthropic"
    capabilities = Capabilities(text=True, structured_output=True,
                                tool_calls=True)

    def available(self) -> bool:
        return bool(self.model and self.api_key)

    def _require_ready(self) -> None:
        if not self.available():
            raise ProviderNotConfigured(
                "Anthropic needs ANTHROPIC_MODEL and ANTHROPIC_API_KEY.")

    def _headers(self) -> dict:
        return {"x-api-key": self.api_key,
                "anthropic-version": API_VERSION,
                "Content-Type": "application/json"}

    @staticmethod
    def _split(messages: list) -> tuple[str | None, list]:
        system = None
        clean = []
        for message in messages:
            role = message.get("role", "user")
            if role == "system" and system is None:
                system = message.get("content", "")
            else:
                clean.append({"role": "assistant" if role == "assistant"
                              else "user",
                              "content": message.get("content", "")})
        return system, clean

    @staticmethod
    def _usage(data: dict) -> tuple[int, int]:
        try:
            usage = data.get("usage", {}) or {}
            return (int(usage.get("input_tokens", 0)),
                    int(usage.get("output_tokens", 0)))
        except (TypeError, ValueError):
            return 0, 0

    @staticmethod
    def _blocks(data: dict) -> list:
        content = data.get("content")
        if not isinstance(content, list) or not content:
            raise ProviderError(
                "Anthropic API returned an unexpected shape.")
        return [b for b in content if isinstance(b, dict)]

    def generate_text(self, messages: list) -> AIResult:
        self._require_ready()
        system, clean = self._split(messages)
        payload: dict = {"model": self.model, "max_tokens": 1000,
                         "messages": clean}
        if system:
            payload["system"] = system
        data = self._post(API_URL, payload, self._headers())
        texts = [b.get("text", "") for b in self._blocks(data)
                 if b.get("type") == "text" and b.get("text")]
        text = "\n".join(texts).strip()
        if not text:
            raise ProviderError("Anthropic API returned an empty reply.")
        tok_in, tok_out = self._usage(data)
        return AIResult(text=text, provider="anthropic", model=self.model,
                        tokens_in=tok_in, tokens_out=tok_out)

    def generate_structured(self, messages: list,
                            json_schema: dict) -> AIResult:
        # No verified native JSON-schema mode: instruct + parse strictly.
        self._require_ready()
        guided = (list(messages)
                  + [{"role": "user",
                      "content": "Reply with ONLY a JSON object matching"
                                 " this schema, no prose: "
                                 + json.dumps(json_schema)[:2000]}])
        result = self.generate_text(guided)
        try:
            parsed = json.loads(result.text)
        except ValueError as exc:
            raise ProviderError(
                "Anthropic API did not return valid JSON.",
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out) from exc
        if not isinstance(parsed, dict):
            raise ProviderError(
                "Anthropic API did not return a JSON object.",
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out)
        return AIResult(text=result.text, provider="anthropic",
                        model=self.model, tokens_in=result.tokens_in,
                        tokens_out=result.tokens_out, structured=parsed)

    def run_tools(self, messages: list, tools: list, handler) -> AIResult:
        self._require_ready()
        wire_tools = [{"name": t["name"],
                       "description": t.get("description", ""),
                       "input_schema": t.get("parameters", {
                           "type": "object", "properties": {}})}
                      for t in tools]
        system, thread = self._split(messages)
        total_in = total_out = 0
        for _ in range(MAX_TOOL_ROUNDS):
            payload: dict = {"model": self.model, "max_tokens": 1000,
                             "messages": thread, "tools": wire_tools}
            if system:
                payload["system"] = system
            data = self._post(API_URL, payload, self._headers())
            tok_in, tok_out = self._usage(data)
            total_in += tok_in
            total_out += tok_out
            blocks = self._blocks(data)
            calls = [ToolCall(id=str(b.get("id", "")),
                              name=str(b.get("name", "")),
                              arguments=dict(b.get("input", {}) or {}),
                              raw_arguments=json.dumps(b.get("input", {})))
                     for b in blocks if b.get("type") == "tool_use"]
            texts = [b.get("text", "") for b in blocks
                     if b.get("type") == "text" and b.get("text")]
            if not calls or data.get("stop_reason") != "tool_use":
                return AIResult(text="\n".join(texts).strip(),
                                provider="anthropic", model=self.model,
                                tokens_in=total_in, tokens_out=total_out)
            thread.append({"role": "assistant", "content": blocks})
            results = []
            for call in calls:
                try:
                    output = handler(call.name, call.arguments)
                except Exception as exc:  # noqa: BLE001 - tool failure is data
                    output = {"error": f"{type(exc).__name__}: {exc}"}
                results.append({"type": "tool_result",
                                "tool_use_id": call.id,
                                "content": json.dumps(output,
                                                      default=str)[:4000]})
            thread.append({"role": "user", "content": results})
        raise ProviderError("Anthropic API: tool loop did not finish in"
                            f" {MAX_TOOL_ROUNDS} rounds.")
