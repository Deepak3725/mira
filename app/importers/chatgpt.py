"""ChatGPT conversations.json importer (mapping graph + fallback).

Source format: Settings -> Data controls -> Export data produces a zip
holding conversations.json: a list of {title, mapping} where mapping
values are nodes with message.author.role and message.content.parts.
Only the owner's (user-role) parts are extracted; create_time becomes
the candidate timestamp; non-text parts are counted as attachments.
"""

from __future__ import annotations

import datetime as dt

from .base import Importer, _Ctx


def _iso_epoch(value) -> str | None:
    try:
        moment = dt.datetime.fromtimestamp(float(value), dt.timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _user_parts(content) -> tuple[list[str], list[str]]:
    texts: list[str] = []
    attachments: list[str] = []
    if not isinstance(content, dict):
        return texts, attachments
    for part in content.get("parts", []) or []:
        if isinstance(part, str) and part.strip():
            texts.append(part)
        elif isinstance(part, dict):
            kind = part.get("content_type") or part.get("type") or "file"
            attachments.append(str(part.get("name") or kind))
    return texts, attachments


class ChatGPTImporter(Importer):
    name = "chatgpt"
    label = "ChatGPT"
    route_hint = "ChatGPT Settings -> Data controls -> Export data (zip)."
    filenames = ("conversations.json",)

    def detect(self, data, filename: str) -> bool:
        if not isinstance(data, list) or not data:
            return False
        first = next((c for c in data if isinstance(c, dict)), None)
        if first is None:
            return False
        return (isinstance(first.get("mapping"), dict)
                or isinstance(first.get("messages"), list))

    def parse_item(self, item, filename: str, ctx: _Ctx) -> None:
        if not isinstance(item, dict):
            ctx.skipped += 1
            return
        title = item.get("title") or "untitled"
        source = f"chatgpt:{str(title)[:120]}"
        mapping = item.get("mapping")
        nodes: list = []
        if isinstance(mapping, dict):
            nodes = [n for n in mapping.values()
                     if isinstance(n, dict)]
        elif isinstance(item.get("messages"), list):
            nodes = [{"message": {"author": {"role": "user"},
                                  "content": {"parts": [m.get("content")
                                                        or m.get("text")
                                                        or ""]}}}
                     for m in item["messages"]
                     if isinstance(m, dict) and m.get("role") == "user"]
        found = False
        for node in nodes:
            message = node.get("message") or {}
            author = (message.get("author") or {}).get("role")
            if author != "user":
                continue
            texts, attachments = _user_parts(message.get("content"))
            stamp = _iso_epoch(message.get("create_time"))
            if attachments:
                ctx.attachments += len(attachments)
            for text in texts:
                found = True
                ctx.add(text, source, source_time=stamp,
                        attachments=tuple(attachments))
                if ctx.full():
                    return
        if not found:
            ctx.skipped += 1
