"""Meta adapter: Chat Completions for text/structured (reusing the
verified legacy client), Responses API for agentic tool loops.

Responses wire verified against the official docs
(https://dev.meta.ai/docs/protocols/responses and
https://dev.meta.ai/docs/tool-calling, 2026-10-31):
POST {API_BASE}/v1/responses with model/input/tools; function_call items
carry call_id/name/arguments; replies are function_call_output items;
usage is input_tokens/output_tokens; tool_choice must be "auto".
Turns chain with previous_response_id (server-managed history).
"""

from __future__ import annotations

import json

from ...meta_client import (
    API_BASE,
    CHAT_PATH,
    MAX_JSON_OUTPUT_TOKENS,
    MAX_OUTPUT_TOKENS,
    MetaAPIError,
    MetaNotConfigured,
    MetaProvider,
)
from ..types import (
    AIResult,
    Capabilities,
    ProviderError,
    ProviderNotConfigured,
    ToolCall,
)
from .base import BaseAdapter

RESPONSES_PATH = "/v1/responses"
MAX_TOOL_ROUNDS = 5


def _map_error(exc: MetaAPIError) -> ProviderError:
    return ProviderError(str(exc), retryable=False,
                         tokens_in=exc.tokens_in,
                         tokens_out=exc.tokens_out)


class MetaAdapter(BaseAdapter):
    name = "meta"
    capabilities = Capabilities(text=True, structured_output=True,
                                tool_calls=True)

    def __init__(self, model: str | None = None,
                 api_key: str | None = None, post=None):
        # Legacy client reads settings when model/key are None.
        self._legacy = MetaProvider(model=model, api_key=api_key,
                                    post=self._adapt_post(post) if post
                                    else None)
        self.model = self._legacy.model
        self.api_key = self._legacy.api_key
        self._post = post

    @staticmethod
    def _adapt_post(post):
        # New seam is post(url, payload, headers); legacy is (url, key, payload).
        def legacy_post(url, key, payload):
            return post(url, payload,
                        {"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"})
        return legacy_post

    def available(self) -> bool:
        try:
            self._legacy._require_ready()
        except MetaNotConfigured:
            return False
        return True

    def _require_ready(self) -> None:
        try:
            self._legacy._require_ready()
        except MetaNotConfigured as exc:
            raise ProviderNotConfigured(str(exc)) from exc

    def generate_text(self, messages: list) -> AIResult:
        self._require_ready()
        payload = {"model": self.model, "messages": list(messages),
                   "max_tokens": MAX_OUTPUT_TOKENS, "temperature": 0.7}
        try:
            if self._post is not None:
                data = self._post(
                    API_BASE + CHAT_PATH, payload,
                    {"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"})
            else:
                data = self._legacy._post(
                    API_BASE + CHAT_PATH, self.api_key, payload)
            text, tok_in, tok_out = MetaProvider._text_and_usage(data)
        except MetaAPIError as exc:
            raise _map_error(exc) from exc
        return AIResult(text=text, provider="meta", model=self.model,
                        tokens_in=tok_in, tokens_out=tok_out)

    def generate_structured(self, messages: list,
                            json_schema: dict) -> AIResult:
        self._require_ready()
        try:
            if self._post is not None:
                payload = {
                    "model": self.model, "messages": list(messages),
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {"name": "result",
                                        "schema": json_schema}},
                    "max_tokens": MAX_JSON_OUTPUT_TOKENS,
                    "temperature": 0.2,
                }
                data = self._post(
                    API_BASE + CHAT_PATH, payload,
                    {"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"})
                text, tok_in, tok_out = MetaProvider._text_and_usage(data)
            else:
                out = self._legacy.generate_json(list(messages),
                                                 json_schema)
                text = json.dumps(out["data"])
                tok_in, tok_out = out["tokens_in"], out["tokens_out"]
                return AIResult(
                    text=text, provider="meta", model=self.model,
                    tokens_in=tok_in, tokens_out=tok_out,
                    structured=out["data"])
        except MetaAPIError as exc:
            raise _map_error(exc) from exc
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise ProviderError("Meta API did not return valid JSON.",
                                tokens_in=tok_in,
                                tokens_out=tok_out) from exc
        if not isinstance(parsed, dict):
            raise ProviderError("Meta API did not return a JSON object.",
                                tokens_in=tok_in, tokens_out=tok_out)
        return AIResult(text=text, provider="meta", model=self.model,
                        tokens_in=tok_in, tokens_out=tok_out,
                        structured=parsed)

    @staticmethod
    def _input_text(role: str, text: str) -> dict:
        return {"role": role,
                "content": [{"type": "input_text", "text": text}]}

    def _responses_post(self, payload: dict) -> dict:
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        if self._post is not None:
            try:
                return self._post(API_BASE + RESPONSES_PATH, payload,
                                  headers)
            except ProviderError:
                raise
            except OSError as exc:
                raise ProviderError(f"Request failed: {exc}.",
                                    retryable=True) from exc
        from .base import http_post_json

        return http_post_json(API_BASE + RESPONSES_PATH, payload,
                              headers)

    def run_tools(self, messages: list, tools: list, handler) -> AIResult:
        """Tool loop over the Responses API (previous_response_id chain)."""
        self._require_ready()
        wire_tools = [{"type": "function", "name": t["name"],
                       "description": t.get("description", ""),
                       "parameters": t.get("parameters", {
                           "type": "object", "properties": {}})}
                      for t in tools]
        history = [self._input_text(m.get("role", "user"),
                                    m.get("content", ""))
                   for m in messages]
        previous_id = None
        total_in = total_out = 0
        texts: list[str] = []
        for _ in range(MAX_TOOL_ROUNDS):
            payload: dict = {"model": self.model, "input": history,
                             "tools": wire_tools, "tool_choice": "auto"}
            if previous_id is not None:
                payload["previous_response_id"] = previous_id
                payload["input"] = history[-1:]
            data = self._responses_post(payload)
            if not isinstance(data, dict) or data.get("object") != "response":
                raise ProviderError("Meta Responses API: unexpected shape.")
            if data.get("status") not in ("completed", None):
                raise ProviderError(
                    f"Meta Responses API status: {data.get('status')}.")
            previous_id = data.get("id")
            usage = data.get("usage", {}) or {}
            try:
                total_in += int(usage.get("input_tokens", 0))
                total_out += int(usage.get("output_tokens", 0))
            except (TypeError, ValueError):
                pass
            calls: list[ToolCall] = []
            progressed = False
            for item in data.get("output", []) or []:
                if not isinstance(item, dict):
                    continue
                kind = item.get("type")
                if kind == "message":
                    for part in item.get("content", []) or []:
                        if (isinstance(part, dict)
                                and part.get("type") == "output_text"
                                and part.get("text")):
                            texts.append(part["text"])
                            progressed = True
                elif kind == "function_call":
                    raw = item.get("arguments", "") or ""
                    try:
                        args = json.loads(raw) if raw else {}
                    except ValueError:
                        args = {}
                    if not isinstance(args, dict):
                        args = {}
                    calls.append(ToolCall(
                        id=str(item.get("call_id", "")),
                        name=str(item.get("name", "")),
                        arguments=args, raw_arguments=raw))
            if not calls:
                break
            progressed = True
            followups = []
            for call in calls:
                try:
                    output = handler(call.name, call.arguments)
                except Exception as exc:  # noqa: BLE001 - tool failure is data
                    output = {"error": f"{type(exc).__name__}: {exc}"}
                followups.append({"type": "function_call_output",
                                  "call_id": call.id,
                                  "output": json.dumps(output,
                                                       default=str)[:4000]})
            history = followups
        else:
            raise ProviderError("Meta Responses API: tool loop did not"
                                f" finish in {MAX_TOOL_ROUNDS} rounds.")
        if not progressed and not texts:
            raise ProviderError("Meta Responses API returned no output.")
        return AIResult(text="\n".join(texts).strip(), provider="meta",
                        model=self.model, tokens_in=total_in,
                        tokens_out=total_out)
