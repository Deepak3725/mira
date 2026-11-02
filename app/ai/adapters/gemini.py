"""Google Gemini adapter (generateContent wire, stdlib urllib).

Wire notes follow Google's public Gemini API (contents/parts,
systemInstruction, responseMimeType/responseSchema, usageMetadata).
No Gemini key is configured in this project, so live calls are NOT
yet verified: the suite exercises this adapter only through injected
fake transports. Consumer Gemini access does not grant API access.

Tool calling is deliberately unsupported until its wire shape is
verified against the vendor docs (honest refusal, not guesswork).
"""

from __future__ import annotations

import json

from ..types import (
    AIResult,
    Capabilities,
    ProviderError,
    ProviderNotConfigured,
)
from .base import BaseAdapter, strict_usage


class GeminiAdapter(BaseAdapter):
    name = "gemini"
    capabilities = Capabilities(text=True, structured_output=True)

    def available(self) -> bool:
        return bool(self.model and self.api_key)

    def _require_ready(self) -> None:
        if not self.available():
            raise ProviderNotConfigured(
                "Gemini needs GEMINI_MODEL and GEMINI_API_KEY.")

    def _url(self) -> str:
        return ("https://generativelanguage.googleapis.com/v1beta/models/"
                f"{self.model}:generateContent")

    def _headers(self) -> dict:
        return {"x-goog-api-key": self.api_key,
                "Content-Type": "application/json"}

    @staticmethod
    def _contents(messages: list) -> tuple[list, dict | None]:
        contents = []
        system = None
        for message in messages:
            role = message.get("role", "user")
            if role == "system" and system is None:
                system = {"parts": [{"text": message.get("content", "")}]}
                continue
            contents.append({
                "role": "model" if role == "assistant" else "user",
                "parts": [{"text": message.get("content", "")}]})
        return contents, system

    @staticmethod
    def _usage(data: dict) -> tuple[int, int]:
        return strict_usage(data, "usageMetadata", "promptTokenCount",
                            "candidatesTokenCount", "Gemini")

    @staticmethod
    def _text(data: dict) -> str:
        try:
            parts = data["candidates"][0]["content"]["parts"]
            texts = [p.get("text", "") for p in parts
                     if isinstance(p, dict) and p.get("text")]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                "Gemini API returned an unexpected shape.") from exc
        text = "\n".join(texts).strip()
        if not text:
            raise ProviderError("Gemini API returned an empty reply.")
        return text

    def generate_text(self, messages: list) -> AIResult:
        self._require_ready()
        contents, system = self._contents(messages)
        payload: dict = {"contents": contents}
        if system:
            payload["systemInstruction"] = system
        data = self._post(self._url(), payload, self._headers())
        text = self._text(data)
        tok_in, tok_out = self._usage(data)
        return AIResult(text=text, provider="gemini", model=self.model,
                        tokens_in=tok_in, tokens_out=tok_out)

    def generate_structured(self, messages: list,
                            json_schema: dict) -> AIResult:
        self._require_ready()
        contents, system = self._contents(messages)
        payload: dict = {
            "contents": contents,
            "generationConfig": {"responseMimeType": "application/json",
                                 "responseSchema": json_schema},
        }
        if system:
            payload["systemInstruction"] = system
        data = self._post(self._url(), payload, self._headers())
        text = self._text(data)
        tok_in, tok_out = self._usage(data)
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise ProviderError("Gemini API did not return valid JSON.",
                                tokens_in=tok_in,
                                tokens_out=tok_out) from exc
        if not isinstance(parsed, dict):
            raise ProviderError("Gemini API did not return a JSON object.",
                                tokens_in=tok_in, tokens_out=tok_out)
        return AIResult(text=text, provider="gemini", model=self.model,
                        tokens_in=tok_in, tokens_out=tok_out,
                        structured=parsed)
