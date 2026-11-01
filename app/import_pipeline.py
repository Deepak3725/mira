"""Knowledge-import entry points (compatibility wrappers).

Real parsing lives in :mod:`app.importers` (one importer per vendor).
These wrappers preserve the original Phase 4 behaviour, error codes,
and messages exactly: the route and the older tests keep working
unchanged while new code calls :func:`app.importers.import_upload`.
"""

from __future__ import annotations

import json

from .importers import ChatGPTImporter, GenericImporter
from .memories import MAX_MEMORY_CHARS, MemoryError  # noqa: F401

MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_CANDIDATES = 100
CHUNK_CHARS = 500

#: Content types accepted for export uploads. Browsers label files
#: inconsistently, so the small JSON/text and zip families are allowed;
#: the parser still validates the actual bytes.
ALLOWED_IMPORT_CONTENT_TYPES = ("application/json", "text/json", "text/plain")
ALLOWED_ARCHIVE_CONTENT_TYPES = ("application/zip",
                                 "application/x-zip-compressed",
                                 "application/x-zip")


def accept_upload(filename: str, content_type: str) -> str | None:
    """Validate an upload envelope. Returns an error code or None if OK."""
    if not (filename or ""):
        return "no-file"
    lowered = filename.lower()
    ctype = (content_type or "").split(";")[0].strip().lower()
    if lowered.endswith(".json"):
        return None if ctype in ALLOWED_IMPORT_CONTENT_TYPES else "bad-file"
    if lowered.endswith(".zip"):
        return None if ctype in ALLOWED_ARCHIVE_CONTENT_TYPES else "bad-file"
    return "bad-file"


def parse_chatgpt_export(raw: bytes) -> list[tuple[str, str]]:
    """Parse conversations.json bytes -> [(content, source)] candidates."""
    if len(raw) > MAX_IMPORT_BYTES:
        raise MemoryError(
            f"File is too large ({len(raw) // 1024} KB). Max is "
            f"{MAX_IMPORT_BYTES // (1024 * 1024)} MB.")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise MemoryError("File is not valid UTF-8 JSON.") from exc
    if isinstance(data, dict) and isinstance(data.get("memories"), list):
        out = GenericImporter().parse_data(data, "legacy.json")
        if not out.candidates:
            raise MemoryError("No importable memories found in this file.")
        return [(c.content, c.source) for c in out.candidates]
    if not isinstance(data, list):
        raise MemoryError(
            "Unsupported file: expected a ChatGPT conversations.json list "
            "or {\"memories\": [...]}.")
    out = ChatGPTImporter().parse_data(data, "conversations.json")
    if not out.candidates:
        raise MemoryError("No importable user messages found in this file.")
    return [(c.content, c.source) for c in out.candidates]
