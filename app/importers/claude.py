"""Claude export importer (conversations + memories + projects).

Source format (verified against a real-export schema survey):
Settings -> Privacy -> Export data emails a zip holding
conversations.json (array of {uuid, name, created_at, updated_at,
chat_messages}), memories.json (one object with conversations_memory
and project memories), and projects.json (projects with docs).
Only the owner's (human-sender) texts are extracted.
"""

from __future__ import annotations

from .base import Importer, _Ctx


def _human_texts(message: dict) -> list[str]:
    if not isinstance(message, dict) or message.get("sender") != "human":
        return []
    text = message.get("text")
    if isinstance(text, str) and text.strip():
        return [text]
    out = []
    content = message.get("content")
    blocks = content if isinstance(content, list) else []
    for block in blocks:
        if (isinstance(block, dict) and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].strip()):
            out.append(block["text"])
    return out


def _attachment_names(message: dict) -> list[str]:
    names: list[str] = []
    for key in ("attachments", "files", "images"):
        items = message.get(key)
        if not isinstance(items, list):
            continue
        for entry in items:
            if isinstance(entry, dict):
                names.append(str(entry.get("name") or entry.get("file_name")
                                 or key))
            elif isinstance(entry, str):
                names.append(entry)
    return names


class ClaudeImporter(Importer):
    name = "claude"
    label = "Claude"
    route_hint = "Claude Settings -> Privacy -> Export data (zip)."
    filenames = ("conversations.json", "memories.json", "projects.json")

    def detect(self, data, filename: str) -> bool:
        lowered = (filename or "").lower()
        if isinstance(data, dict) and (
                "conversations_memory" in data
                or "project_memories" in data):
            return True
        if not isinstance(data, list) or not data:
            return "claude" in lowered and lowered.endswith(".json")
        first = next((c for c in data if isinstance(c, dict)), None)
        if first is None:
            return False
        if isinstance(first.get("chat_messages"), list):
            return True
        return ("memories.json" in lowered or "projects.json" in lowered
                or "conversations.json" in lowered and "mapping" not in first)

    def parse_item(self, item, filename: str, ctx: _Ctx) -> None:
        if not isinstance(item, dict):
            ctx.skipped += 1
            return
        if isinstance(item.get("chat_messages"), list):
            self._parse_conversation(item, ctx)
        elif "conversations_memory" in item or "project_memories" in item:
            self._parse_memories(item, ctx)
        elif "docs" in item or "prompt_template" in item:
            self._parse_project(item, ctx)
        else:
            ctx.skipped += 1

    def _parse_conversation(self, convo: dict, ctx: _Ctx) -> None:
        name = convo.get("name") or convo.get("uuid") or "untitled"
        source = f"claude:{str(name)[:120]}"
        fallback = convo.get("created_at") or convo.get("updated_at")
        found = False
        for message in convo.get("chat_messages") or []:
            for text in _human_texts(message):
                found = True
                names = _attachment_names(message)
                if names:
                    ctx.attachments += len(names)
                stamp = (message.get("created_at")
                         or message.get("updated_at") or fallback)
                ctx.add(text, source, source_time=str(stamp) if stamp
                        else None, attachments=tuple(names))
                if ctx.full():
                    return
        if not found:
            ctx.skipped += 1

    def _parse_memories(self, data: dict, ctx: _Ctx) -> None:
        general = data.get("conversations_memory")
        if isinstance(general, str) and general.strip():
            ctx.add(general, "claude:memory")
        projects = data.get("project_memories")
        if isinstance(projects, dict):
            for key, value in projects.items():
                if isinstance(value, str) and value.strip():
                    ctx.add(value, f"claude:memory:{str(key)[:60]}")
                if ctx.full():
                    return

    def _parse_project(self, project: dict, ctx: _Ctx) -> None:
        name = project.get("name") or "project"
        source = f"claude:project:{str(name)[:100]}"
        for key in ("description", "prompt_template"):
            value = project.get(key)
            if isinstance(value, str) and value.strip():
                ctx.add(value, source)
                if ctx.full():
                    return
        docs = project.get("docs")
        if isinstance(docs, list):
            for doc in docs:
                if not isinstance(doc, dict):
                    continue
                body = doc.get("content") or doc.get("text") or ""
                if isinstance(body, str) and body.strip():
                    title = doc.get("title") or doc.get("name") or ""
                    ctx.add(body, f"{source}:{str(title)[:40]}".rstrip(":"))
                    if ctx.full():
                        return
