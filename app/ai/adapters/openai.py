"""OpenAI adapter (Chat Completions wire, stdlib urllib).

Wire notes follow OpenAI's public Chat Completions API (chat/completions
with messages/tools, usage.prompt/completion_tokens). No OpenAI key is
configured in this project, so live calls are NOT yet verified: the
suite exercises this adapter only through injected fake transports.
Consumer ChatGPT subscriptions do not grant API access.
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
from .base import BaseAdapter, strict_usage

API_URL = "https://api.openai.com/v1/chat/completions"
MAX_TOOL_ROUNDS = 5


class OpenAIAdapter(BaseAdapter):
    name = "openai"
    capabilities = Capabilities(text=True, structured_output=True,
                                tool_calls=True)

    def available(self) -> bool:
        return bool(self.model and self.api_key)

    def _require_ready(self) -> None:
        if not self.available():
            raise ProviderNotConfigured(
                "OpenAI needs OPENAI_MODEL and OPENAI_API_KEY.")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"}

    @staticmethod
    def _usage(data: dict) -> tuple[int, int]:
        return strict_usage(data, "usage", "prompt_tokens",
                            "completion_tokens", "OpenAI")

    @staticmethod
    def _message(data: dict) -> dict:
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                "OpenAI API returned an unexpected shape.") from exc
        if not isinstance(message, dict):
            raise ProviderError(
                "OpenAI API returned an unexpected shape.")
        return message

    def generate_text(self, messages: list) -> AIResult:
        self._require_ready()
        data = self._post(API_URL, {
            "model": self.model, "messages": list(messages),
            "max_tokens": 1000, "temperature": 0.7}, self._headers())
        message = self._message(data)
        text = (message.get("content") or "").strip()
        if not text:
            raise ProviderError("OpenAI API returned an empty reply.")
        tok_in, tok_out = self._usage(data)
        return AIResult(text=text, provider="openai", model=self.model,
                        tokens_in=tok_in, tokens_out=tok_out)

    def generate_structured(self, messages: list,
                            json_schema: dict) -> AIResult:
        self._require_ready()
        data = self._post(API_URL, {
            "model": self.model, "messages": list(messages),
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "result",
                                "schema": json_schema}},
            "max_tokens": 2000, "temperature": 0.2}, self._headers())
        message = self._message(data)
        text = (message.get("content") or "").strip()
        tok_in, tok_out = self._usage(data)
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise ProviderError("OpenAI API did not return valid JSON.",
                                tokens_in=tok_in,
                                tokens_out=tok_out) from exc
        if not isinstance(parsed, dict):
            raise ProviderError("OpenAI API did not return a JSON object.",
                                tokens_in=tok_in, tokens_out=tok_out)
        return AIResult(text=text, provider="openai", model=self.model,
                        tokens_in=tok_in, tokens_out=tok_out,
                        structured=parsed)

    def run_tools(self, messages: list, tools: list, handler) -> AIResult:
        self._require_ready()
        wire_tools = [{"type": "function",
                       "function": {
                           "name": t["name"],
                           "description": t.get("description", ""),
                           "parameters": t.get("parameters", {
                               "type": "object", "properties": {}})}}
                      for t in tools]
        thread = list(messages)
        total_in = total_out = 0
        for _ in range(MAX_TOOL_ROUNDS):
            data = self._post(API_URL, {
                "model": self.model, "messages": thread,
                "tools": wire_tools, "tool_choice": "auto",
                "max_tokens": 1000, "temperature": 0.7},
                self._headers())
            message = self._message(data)
            try:
                tok_in, tok_out = self._usage(data)
            except ProviderError as exc:
                # Earlier rounds parsed fine: keep their real usage
                # and flag this round uncertain (mirrors Meta).
                raise ProviderError(
                    str(exc), tokens_in=total_in,
                    tokens_out=total_out,
                    uncertain_extra=True) from exc
            total_in += tok_in
            total_out += tok_out
            calls = []
            for raw in message.get("tool_calls", []) or []:
                fn = (raw.get("function") or {}) if isinstance(raw, dict) else {}
                try:
                    args = json.loads(fn.get("arguments", "") or "")
                except ValueError:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                calls.append(ToolCall(
                    id=str(raw.get("id", "")),
                    name=str(fn.get("name", "")),
                    arguments=args,
                    raw_arguments=str(fn.get("arguments", "") or "")))
            if not calls:
                text = (message.get("content") or "").strip()
                return AIResult(text=text, provider="openai",
                                model=self.model, tokens_in=total_in,
                                tokens_out=total_out)
            thread.append({"role": "assistant",
                           "content": message.get("content") or "",
                           "tool_calls": [
                               {"id": c.id, "type": "function",
                                "function": {"name": c.name,
                                             "arguments": c.raw_arguments}}
                               for c in calls]})
            for call in calls:
                try:
                    output = handler(call.name, call.arguments)
                except Exception as exc:  # noqa: BLE001 - tool failure is data
                    output = {"error": f"{type(exc).__name__}: {exc}"}
                thread.append({"role": "tool", "tool_call_id": call.id,
                               "content": json.dumps(output, default=str)[:4000]})
        raise ProviderError("OpenAI API: tool loop did not finish in"
                            f" {MAX_TOOL_ROUNDS} rounds.",
                            tokens_in=total_in, tokens_out=total_out)
