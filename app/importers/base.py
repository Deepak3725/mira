"""Importer interface plus shared candidate handling.

Every importer extracts the OWNER's own words (never assistant replies,
never other people's messages), chunks long texts, dedupes identical
items silently, and reports near-duplicates as review warnings.
"""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field

from ..memories import MAX_MEMORY_CHARS, MemoryError
from .classify import classify
from .streaming import iter_json_array_items
from .types import Candidate, ImportResult

MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_CANDIDATES = 100
CHUNK_CHARS = 500

_WORD = re.compile(r"[a-z0-9']+")


def chunks(text: str) -> list[str]:
    """Split long texts into reviewable pieces (sentence-aware)."""
    text = " ".join((text or "").split())
    if len(text) <= CHUNK_CHARS:
        return [text] if text else []
    parts, current = [], ""
    for sentence in text.replace("! ", ". ").replace("? ", ". ").split(". "):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(current) + len(sentence) + 2 > CHUNK_CHARS and current:
            parts.append(current)
            current = sentence
        else:
            current = f"{current}. {sentence}" if current else sentence
    if current:
        parts.append(current)
    return [p[:MAX_MEMORY_CHARS] for p in parts if p]


def tokens(text: str) -> set[str]:
    return {t for t in _WORD.findall((text or "").lower()) if len(t) > 2}


@dataclass
class _Ctx:
    candidates: list[Candidate] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    skipped: int = 0
    attachments: int = 0

    def full(self) -> bool:
        return len(self.candidates) >= MAX_CANDIDATES

    def add(self, text: str, source: str, *,
            source_time: str | None = None,
            attachments: tuple[str, ...] = ()) -> None:
        for chunk in chunks(text):
            norm = " ".join(chunk.lower().split())
            if not norm or norm in self.seen:
                continue
            self.seen.add(norm)
            self.candidates.append(Candidate(
                content=chunk[:MAX_MEMORY_CHARS],
                source=str(source)[:200],
                category=classify(chunk),
                source_time=(str(source_time).strip()[:64]
                             if source_time else None),
                attachments=tuple(a[:120] for a in attachments)))
            if self.full():
                return


def find_conflicts(candidates: list[Candidate],
                   limit: int = 10) -> list[str]:
    """Flag near-duplicate pairs (overlap>=0.6, not identical) for review."""
    warnings: list[str] = []
    tokenized = [tokens(c.content) for c in candidates]
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            first, second = tokenized[i], tokenized[j]
            if not first or not second:
                continue
            overlap = len(first & second) / max(len(first), len(second))
            if overlap < 0.6:
                continue
            norm = lambda text: " ".join(text.lower().split())  # noqa: E731
            if norm(candidates[i].content) == norm(candidates[j].content):
                continue
            warnings.append(
                f"Possible overlap between item {i + 1} "
                f"({candidates[i].source}) and item {j + 1} "
                f"({candidates[j].source}): review both.")
            if len(warnings) >= limit:
                return warnings
    return warnings


class Importer:
    """One export format. Subclasses implement detect + parse_item."""

    name = "base"
    label = "Base"
    route_hint = ""
    filenames: tuple[str, ...] = ()

    def detect(self, data, filename: str) -> bool:
        """True when this importer handles the parsed payload."""
        raise NotImplementedError

    def parse_item(self, item, filename: str, ctx: _Ctx) -> None:
        """Extract candidates from one top-level element."""
        raise NotImplementedError

    def parse_data(self, data, filename: str) -> ImportResult:
        """Parse an already-decoded JSON payload."""
        ctx = _Ctx()
        if isinstance(data, list):
            for item in data:
                try:
                    self.parse_item(item, filename, ctx)
                except Exception:  # noqa: BLE001 - one bad item skips
                    ctx.skipped += 1
                if ctx.full():
                    break
        else:
            self.parse_item(data, filename, ctx)
        return ImportResult(provider=self.name,
                            candidates=ctx.candidates,
                            skipped=ctx.skipped,
                            attachments=ctx.attachments)

    def parse_stream(self, stream, filename: str) -> ImportResult:
        """Parse incrementally, stopping once enough was extracted."""
        ctx = _Ctx()
        for raw, _consumed in iter_json_array_items(stream):
            try:
                item = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                ctx.skipped += 1
                continue
            try:
                self.parse_item(item, filename, ctx)
            except Exception:  # noqa: BLE001 - one bad item skips
                ctx.skipped += 1
            if ctx.full():
                break
        return ImportResult(provider=self.name,
                            candidates=ctx.candidates,
                            skipped=ctx.skipped,
                            attachments=ctx.attachments)


def parse_bytes_with(importer: Importer, raw: bytes,
                     filename: str) -> ImportResult:
    """Size-checked entry for one JSON document."""
    if len(raw) > MAX_IMPORT_BYTES:
        raise MemoryError(
            f"File is too large ({len(raw) // 1024} KB). Max is "
            f"{MAX_IMPORT_BYTES // (1024 * 1024)} MB.")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise MemoryError("File is not valid UTF-8 JSON.") from exc
    return importer.parse_data(data, filename)


def parse_stream_with(importer: Importer, raw: bytes,
                      filename: str) -> ImportResult:
    """Incremental entry for one JSON document (bounded memory)."""
    if len(raw) > MAX_IMPORT_BYTES:
        raise MemoryError(
            f"File is too large ({len(raw) // 1024} KB). Max is "
            f"{MAX_IMPORT_BYTES // (1024 * 1024)} MB.")
    try:
        return importer.parse_stream(io.BytesIO(raw), filename)
    except UnicodeDecodeError as exc:
        raise MemoryError("File is not valid UTF-8 JSON.") from exc
