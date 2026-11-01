"""Generic {"memories": [...]} importer (hand-made files, fixtures)."""

from __future__ import annotations

from .base import Importer, _Ctx
from .types import ImportResult


class GenericImporter(Importer):
    name = "generic"
    label = "Generic JSON"
    route_hint = "A hand-made {\"memories\": [...]} JSON file."
    filenames = ()

    def detect(self, data, filename: str) -> bool:
        return (isinstance(data, dict)
                and isinstance(data.get("memories"), list))

    def parse_item(self, item, filename: str, ctx: _Ctx) -> None:
        if not isinstance(item, dict):
            ctx.skipped += 1
            return
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            ctx.skipped += 1
            return
        source = item.get("source") or "manual-import"
        ctx.add(content, source,
                source_time=item.get("time") or item.get("source_time"))

    def parse_data(self, data, filename: str) -> ImportResult:
        ctx = _Ctx()
        items = data.get("memories") if isinstance(data, dict) else []
        for item in items if isinstance(items, list) else []:
            self.parse_item(item, filename, ctx)
            if ctx.full():
                break
        return ImportResult(provider=self.name,
                            candidates=ctx.candidates,
                            skipped=ctx.skipped,
                            attachments=ctx.attachments)
