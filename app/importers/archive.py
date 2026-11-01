"""Safe export-archive handling (zip only, never extracted to disk).

Guards: member count, per-member and total decompressed size (zip-bomb
protection), and an allowlist of known export members. Takeout archives
are multi-product: only the Gemini Apps activity file is wanted.
"""

from __future__ import annotations

import io
import zipfile

from ..memories import MemoryError

MAX_MEMBERS = 50
MAX_MEMBER_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_INCLUDED = 10

#: Basename (lowercased) -> provider hint, in preference order.
KNOWN_MEMBERS = (
    ("conversations.json", "chatgpt-or-claude"),
    ("myactivity.json", "gemini"),
    ("memories.json", "claude"),
    ("projects.json", "claude"),
)


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1].lower()


def extract_members(raw: bytes) -> list[tuple[str, bytes]]:
    """Return [(member_name, bytes)] for supported export members."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise MemoryError("File is not a valid zip archive.") from exc
    with archive:
        infos = [i for i in archive.infolist() if not i.is_dir()]
        if len(infos) > MAX_MEMBERS:
            raise MemoryError(
                f"Archive holds too many files ({len(infos)}).")
        wanted: list = []
        for info in infos:
            base = _basename(info.filename)
            known = next(
                (hint for name, hint in KNOWN_MEMBERS if name == base),
                None)
            if known is None:
                continue
            if base == "myactivity.json" and "gemini" not in (
                    info.filename.lower()):
                # Takeout ships one activity file per Google product.
                continue
            if info.file_size > MAX_MEMBER_BYTES:
                raise MemoryError(
                    f"Archive member {info.filename} is too large.")
            wanted.append(info)
        if not wanted:
            raise MemoryError(
                "Archive has no supported export"
                " (conversations.json, MyActivity.json, memories.json).")
        total = sum(i.file_size for i in wanted)
        if total > MAX_TOTAL_BYTES:
            raise MemoryError("Archive content is too large to import.")
        out: list[tuple[str, bytes]] = []
        for info in wanted[:MAX_INCLUDED]:
            out.append((info.filename, archive.read(info.filename)))
        return out
