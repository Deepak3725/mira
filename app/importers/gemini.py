"""Gemini Takeout importer (My Activity records).

Source format (verified against a real-export analysis): Google
Takeout -> My Activity -> Gemini Apps -> MyActivity.json, an array of
records shaped {"header": "Gemini Apps", "title": "Prompted <text>",
"time": ISO-8601, "safeHtmlItem": [...], ...}. Only "Prompted ..."
records are conversations; other verbs are skipped and counted.
Records carry no conversation ids, so sources are grouped by UTC day
-- a documented stand-in, not a thread reconstruction. Only the
owner's prompt text is imported; responses stay out.
"""

from __future__ import annotations

from .base import Importer, _Ctx

PROMPT_VERB = "Prompted"


class GeminiImporter(Importer):
    name = "gemini"
    label = "Gemini (Takeout)"
    route_hint = "Google Takeout -> My Activity -> Gemini Apps."
    filenames = ("myactivity.json",)

    def detect(self, data, filename: str) -> bool:
        lowered = (filename or "").lower()
        if not isinstance(data, list) or not data:
            return lowered.endswith("myactivity.json")
        first = next((r for r in data if isinstance(r, dict)), None)
        if first is None:
            return False
        return (first.get("header") == "Gemini Apps"
                and isinstance(first.get("safeHtmlItem"), list))

    def parse_item(self, item, filename: str, ctx: _Ctx) -> None:
        if not isinstance(item, dict):
            ctx.skipped += 1
            return
        if item.get("header") != "Gemini Apps":
            ctx.skipped += 1
            return
        title = item.get("title") or ""
        if not isinstance(title, str) or not title.startswith(PROMPT_VERB):
            ctx.skipped += 1
            return
        prompt = title[len(PROMPT_VERB):].strip()
        stamp = item.get("time")
        day = str(stamp)[:10] if stamp else "undated"
        attachments = 0
        for key in ("imageFile", "attachedFiles"):
            files = item.get(key)
            if isinstance(files, list):
                attachments += len(files)
        if attachments:
            ctx.attachments += attachments
        if not prompt:
            # Image-only prompt: nothing textual to import.
            ctx.skipped += 1
            return
        source = f"gemini:{day}"
        ctx.add(prompt, source,
                source_time=str(stamp) if stamp else None)
