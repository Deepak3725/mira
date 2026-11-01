"""Bounded incremental reader for top-level JSON arrays.

Large exports are scanned item by item instead of building one giant
object graph: at most MAX_STREAM_BYTES are buffered, each element is
decoded separately, and callers can stop early once they have enough
candidates. Malformed elements are skipped and counted, never fatal.
"""

from __future__ import annotations

from ..memories import MemoryError

MAX_STREAM_BYTES = 5 * 1024 * 1024 + 1


def iter_json_array_items(stream, max_bytes: int = MAX_STREAM_BYTES):
    """Yield (raw_bytes, bytes_consumed) for each top-level element.

    ``stream`` needs only read(). Raises MemoryError when the cap is
    exceeded, the payload is not a JSON array, or it is truncated.
    """
    data = stream.read(max_bytes)
    if len(data) >= max_bytes:
        raise MemoryError(
            f"File is too large ({len(data) // 1024} KB). Max is "
            f"{(max_bytes - 1) // (1024 * 1024)} MB.")
    text = data.decode("utf-8", errors="strict") if isinstance(
        data, (bytes, bytearray)) else data
    pos = 0
    end = len(text)
    while pos < end and text[pos] in " \t\r\n\ufeff":
        pos += 1
    if pos >= end or text[pos] != "[":
        raise MemoryError("Unsupported file: expected a JSON array.")
    pos += 1
    depth = 0
    in_string = False
    escaped = False
    item_start: int | None = None
    string_item = False
    index = pos
    while index < end:
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
                if string_item and depth == 0 and item_start is not None:
                    raw = text[item_start:index + 1]
                    yield raw.encode("utf-8"), index + 1
                    item_start = None
                    string_item = False
        elif char == '"':
            if depth == 0 and item_start is None:
                item_start = index
                string_item = True
            in_string = True
        elif char in "[{":
            if depth == 0 and item_start is None:
                item_start = index
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth < 0:
                if item_start is not None:
                    raise MemoryError("File is not valid JSON: truncated.")
                rest = text[index + 1:]
                if rest.strip(" \t\r\n"):
                    raise MemoryError(
                        "File is not valid JSON: trailing data.")
                return
            if depth == 0 and item_start is not None:
                raw = text[item_start:index + 1]
                yield raw.encode("utf-8"), index + 1
                item_start = None
        elif char == "," and depth == 0 and item_start is None:
            pass
        elif depth == 0 and item_start is None and char not in " \t\r\n":
            # Scalars (numbers, true, ...) as elements: capture verbatim.
            item_start = index
            while (index < end and text[index] not in ",]"
                   and text[index] not in " \t\r\n"):
                index += 1
            raw = text[item_start:index].strip()
            if raw:
                yield raw.encode("utf-8"), index
            item_start = None
            continue
        index += 1
    # Input exhausted without the closing bracket: truncated.
    raise MemoryError("File is not valid JSON: truncated.")
