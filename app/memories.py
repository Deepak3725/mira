"""Approved-memory storage and keyword retrieval (Phase 4).

Owners import candidate facts (see import_pipeline), then review each one:
approve (optionally edited), reject, or delete. Only APPROVED memories are
ever used, and retrieval is plain keyword overlap in Python -- no paid
embeddings, no vector database.
"""

from __future__ import annotations

import re
from sqlite3 import Connection, Row

from .db import insert_returning_id

STATUSES = ("pending", "approved", "rejected")
MAX_MEMORY_CHARS = 2000
#: Knowledge categories assigned at import; unknown values fold to general.
CATEGORIES = ("skill", "experience", "preference", "project", "interest",
              "style", "general")

_WORD = re.compile(r"[a-z0-9']+")


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _tokens(text: str) -> set[str]:
    return {t for t in _WORD.findall(text.lower()) if len(t) > 2}


class MemoryError(Exception):
    """User-facing memory/import error."""


def create_memory(conn: Connection, *, twin_id: int, content: str,
                  source: str = "manual", status: str = "pending",
                  category: str = "general",
                  source_time: str | None = None) -> int | None:
    """Insert a memory. Returns None when it duplicates an existing one."""
    content = content.strip()
    if not content:
        raise MemoryError("Memory cannot be empty.")
    if len(content) > MAX_MEMORY_CHARS:
        raise MemoryError(f"Memory is too long (max {MAX_MEMORY_CHARS} chars).")
    if status not in STATUSES:
        raise MemoryError(f"Unknown status: {status}.")
    if category not in CATEGORIES:
        category = "general"
    stamp = None
    if source_time is not None:
        stamp = str(source_time).strip()[:64] or None
    norm = _normalize(content)
    for row in conn.execute(
        "SELECT content FROM memories WHERE twin_id = ? AND status != 'rejected'",
        (twin_id,),
    ):
        if _normalize(row["content"]) == norm:
            return None
    return insert_returning_id(
        conn,
        "INSERT INTO memories (twin_id, content, source, status, category,"
        " source_time) VALUES (?, ?, ?, ?, ?, ?)",
        (twin_id, content, source[:200], status, category, stamp),
    )


def get_memory(conn: Connection, mem_id: int) -> Row | None:
    return conn.execute("SELECT * FROM memories WHERE id = ?", (mem_id,)).fetchone()


def list_memories(conn: Connection, twin_id: int, status: str | None = None,
                  category: str | None = None) -> list[Row]:
    query = "SELECT * FROM memories WHERE twin_id = ?"
    params: list = [twin_id]
    if status is not None:
        query += " AND status = ?"
        params.append(status)
    if category in CATEGORIES:
        query += " AND category = ?"
        params.append(category)
    query += " ORDER BY id"
    return list(conn.execute(query, params))


def set_status(conn: Connection, mem_id: int, status: str,
               new_content: str | None = None) -> None:
    if status not in STATUSES:
        raise MemoryError(f"Unknown status: {status}.")
    if new_content is not None:
        new_content = new_content.strip()
        if not new_content:
            raise MemoryError("Memory cannot be empty.")
        if len(new_content) > MAX_MEMORY_CHARS:
            raise MemoryError(f"Memory is too long (max {MAX_MEMORY_CHARS} chars).")
        conn.execute("UPDATE memories SET content = ?, status = ? WHERE id = ?",
                     (new_content, status, mem_id))
    else:
        conn.execute("UPDATE memories SET status = ? WHERE id = ?", (status, mem_id))


def delete_memory(conn: Connection, mem_id: int) -> None:
    conn.execute("DELETE FROM memories WHERE id = ?", (mem_id,))


def relevant_memories(conn: Connection, twin_id: int, query: str, limit: int = 3) -> list[str]:
    """Top approved memories by keyword overlap. Deterministic order."""
    qwords = _tokens(query or "")
    if not qwords:
        return []
    scored = []
    for row in list_memories(conn, twin_id, "approved"):
        overlap = len(qwords & _tokens(row["content"]))
        if overlap > 0:
            scored.append((overlap, row["id"], row["content"]))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [content for _, _, content in scored[: max(0, limit)]]
