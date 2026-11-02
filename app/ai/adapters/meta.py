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
    MAX_CHAT_TOKENS,
    MAX_JSON_OUTPUT_TOKENS,
    MAX_OUTPUT_TOKENS,
    MetaAPIError,
    MetaNotConfigured,
    MetaProvider,
    check_output_cap,
    response_format_block,
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
#: Re-exported ceiling for explicit chat output-cap overrides (None
#: keeps the 1500 default, the measured reasoning floor). Bounded calls
#: may request up to this; the smoke test passes 64.
MAX_OVERRIDE_TOKENS = MAX_CHAT_TOKENS
#: Output cap sent on every Responses request (chat parity). Whether the
#: server enforces it is unverifiable without a live call; client-side
#: sending is covered by regression test.
MAX_RESPONSES_OUTPUT_TOKENS = 300


def _map_error(exc: MetaAPIError) -> ProviderError:
    # Truncation is the one proven-transient Meta failure: identical
    # inputs truncate on one attempt and succeed on the next (live
    # runs 17-21), so a bounded retry may recover it. Parse and usage
    # errors are deterministic and stay non-retryable. No caller
    # changes its attempt budget here; eval and interview stay at 1.
    transient = "truncated" in str(exc).lower()
    return ProviderError(str(exc), retryable=transient,
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

    def generate_text(self, messages: list,
                      max_tokens: int | None = None) -> AIResult:
        self._require_ready()
        cap = check_output_cap(max_tokens, default=MAX_OUTPUT_TOKENS)
        payload = {"model": self.model, "messages": list(messages),
                   "max_tokens": cap, "temperature": 0.7}
        try:
            if self._post is not None:
                data = self._post(
                    API_BASE + CHAT_PATH, payload,
                    {"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"})
            else:
                data = self._legacy._post(
                    API_BASE + CHAT_PATH, self.api_key, payload)
            text, tok_in, tok_out, finish, reasoning = \
                MetaProvider._text_and_usage(data)
        except MetaAPIError as exc:
            raise _map_error(exc) from exc
        return AIResult(text=text, provider="meta", model=self.model,
                        tokens_in=tok_in, tokens_out=tok_out,
                        finish_reason=finish,
                        reasoning_tokens=reasoning)

    def generate_structured(self, messages: list,
                            json_schema: dict) -> AIResult:
        self._require_ready()
        try:
            if self._post is not None:
                payload = {
                    "model": self.model, "messages": list(messages),
                    "response_format": response_format_block(
                        "result", json_schema),
                    "max_tokens": MAX_JSON_OUTPUT_TOKENS,
                    "temperature": 0.2,
                }
                data = self._post(
                    API_BASE + CHAT_PATH, payload,
                    {"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"})
                text, tok_in, tok_out, finish, reasoning = \
                    MetaProvider._text_and_usage(data)
            else:
                out = self._legacy.generate_json(list(messages),
                                                 json_schema)
                text = json.dumps(out["data"])
                tok_in, tok_out = out["tokens_in"], out["tokens_out"]
                finish, reasoning = (out.get("finish_reason"),
                                     out.get("reasoning_tokens"))
                return AIResult(
                    text=text, provider="meta", model=self.model,
                    tokens_in=tok_in, tokens_out=tok_out,
                    structured=out["data"], finish_reason=finish,
                    reasoning_tokens=reasoning)
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
                        structured=parsed, finish_reason=finish,
                        reasoning_tokens=reasoning)

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
        uncertain_rounds = 0
        texts: list[str] = []

        def _fail(message: str, *, retryable: bool = False,
                 uncertain: bool = False) -> ProviderError:
            # Failures keep every parsed round's real usage; rounds with
            # missing or malformed usage are flagged uncertain so the
            # caller accounts them conservatively instead of dropping
            # them.
            return ProviderError(
                message, retryable=retryable,
                tokens_in=total_in, tokens_out=total_out,
                uncertain_extra=uncertain or uncertain_rounds > 0)

        for _ in range(MAX_TOOL_ROUNDS):
            payload: dict = {
                "model": self.model, "input": history,
                "tools": wire_tools, "tool_choice": "auto",
                "max_output_tokens": MAX_RESPONSES_OUTPUT_TOKENS,
            }
            if previous_id is not None:
                payload["previous_response_id"] = previous_id
                payload["input"] = history[-1:]
            try:
                data = self._responses_post(payload)
            except ProviderError as exc:
                if exc.usage_known:
                    # The failed round still reported real usage: fold
                    # it into the known partial totals.
                    total_in += exc.tokens_in or 0
                    total_out += exc.tokens_out or 0
                    raise _fail(str(exc),
                               retryable=exc.retryable) from exc
                uncertain_rounds += 1
                raise _fail(str(exc), retryable=exc.retryable,
                           uncertain=True) from exc
            if not isinstance(data, dict) or data.get("object") != "response":
                uncertain_rounds += 1
                raise _fail("Meta Responses API: unexpected shape.",
                           uncertain=True)
            if data.get("status") not in ("completed", None):
                uncertain_rounds += 1
                raise _fail(
                    f"Meta Responses API status: {data.get('status')}.",
                    uncertain=True)
            previous_id = data.get("id")
            usage = data.get("usage", {}) or {}
            round_usage_ok = True
            for key, is_input in (("input_tokens", True),
                                 ("output_tokens", False)):
                if key not in usage:
                    round_usage_ok = False
                    continue
                try:
                    delta = usage[key]
                    if isinstance(delta, bool):
                        round_usage_ok = False
                        continue
                    delta = int(delta)
                except (TypeError, ValueError):
                    round_usage_ok = False
                    continue
                if delta < 0:
                    round_usage_ok = False
                    continue
                if delta > 0:
                    if is_input:
                        total_in += delta
                    else:
                        total_out += delta
            if not round_usage_ok:
                uncertain_rounds += 1
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
            raise _fail("Meta Responses API: tool loop did not"
                        f" finish in {MAX_TOOL_ROUNDS} rounds.")
        if not progressed and not texts:
            raise _fail("Meta Responses API returned no output.")
        return AIResult(text="\n".join(texts).strip(), provider="meta",
                        model=self.model, tokens_in=total_in,
                        tokens_out=total_out)
