"""Importer registry: detect the format, run the right parser.

Single entry points: import_bytes() for one JSON document,
import_upload() for a .json file or a .zip archive (members merged).
Near-duplicate warnings are attached for owner review; nothing is
ever approved automatically.
"""

from __future__ import annotations

import io
import json

from ..memories import MemoryError
from .archive import extract_members
from .base import (
    MAX_CANDIDATES,
    MAX_IMPORT_BYTES,
    Importer,
    find_conflicts,
    parse_bytes_with,
    parse_stream_with,
)
from .chatgpt import ChatGPTImporter
from .claude import ClaudeImporter
from .gemini import GeminiImporter
from .generic import GenericImporter
from .meta_ai import MetaAIImporter
from .types import ImportResult

IMPORTERS: tuple[Importer, ...] = (
    ChatGPTImporter(),
    ClaudeImporter(),
    GeminiImporter(),
    GenericImporter(),
    MetaAIImporter(),
)

SUPPORTED_BLURB = ("conversations.json (ChatGPT mapping or Claude "
                   "chat_messages), MyActivity.json (Gemini Takeout), "
                   "memories.json / projects.json (Claude), or "
                   "{\"memories\": [...]}")


def detect(data, filename: str) -> Importer | None:
    for importer in IMPORTERS:
        if importer.detect(data, filename):
            return importer
    return None


def _decode(raw: bytes):
    if len(raw) > MAX_IMPORT_BYTES:
        raise MemoryError(
            f"File is too large ({len(raw) // 1024} KB). Max is "
            f"{MAX_IMPORT_BYTES // (1024 * 1024)} MB.")
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise MemoryError("File is not valid UTF-8 JSON.") from exc


def import_bytes(raw: bytes, filename: str) -> ImportResult:
    """Parse one JSON document with the detected importer."""
    data = _decode(raw)
    importer = detect(data, filename)
    if importer is None:
        raise MemoryError(f"Unsupported file: expected {SUPPORTED_BLURB}.")
    if isinstance(importer, MetaAIImporter):
        importer.parse_data(data, filename)  # always raises guidance
    if isinstance(data, list):
        result = parse_stream_with(importer, raw, filename)
    else:
        result = importer.parse_data(data, filename)
    if not result.candidates:
        raise MemoryError("No importable owner messages found in this file.")
    result.warnings.extend(find_conflicts(result.candidates))
    return result


def import_upload(filename: str, raw: bytes) -> ImportResult:
    """Parse a .json export or a .zip archive of exports."""
    lowered = (filename or "").lower()
    if lowered.endswith(".zip"):
        merged = ImportResult(provider="mixed")
        seen: set[str] = set()
        for member_name, member_raw in extract_members(raw):
            try:
                data = _decode(member_raw)
            except MemoryError:
                merged.skipped += 1
                continue
            importer = detect(data, member_name)
            if importer is None or isinstance(importer, MetaAIImporter):
                merged.skipped += 1
                continue
            part = importer.parse_data(data, member_name)
            if merged.provider == "mixed" and part.candidates:
                merged.provider = part.provider
            for candidate in part.candidates:
                norm = " ".join(candidate.content.lower().split())
                if norm in seen:
                    continue
                seen.add(norm)
                merged.candidates.append(candidate)
                if len(merged.candidates) >= MAX_CANDIDATES:
                    break
            merged.skipped += part.skipped
            merged.attachments += part.attachments
            if len(merged.candidates) >= MAX_CANDIDATES:
                break
        if not merged.candidates:
            raise MemoryError(
                "No importable owner messages found in this archive.")
        merged.warnings.extend(find_conflicts(merged.candidates))
        return merged
    if not lowered.endswith(".json"):
        raise MemoryError(f"Unsupported file: expected {SUPPORTED_BLURB}.")
    return import_bytes(raw, filename)
