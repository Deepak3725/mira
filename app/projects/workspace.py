"""Shared project workspace: versioned files in the database.

Writes never overwrite: every save appends a new version row, so any
earlier state can be restored and reviewed. Paths are jailed to the
project (no absolute paths, no parent traversal).
"""

from __future__ import annotations

from ..db import insert_returning_id
from .graph import ProjectError

MAX_PATH_CHARS = 200
MAX_FILE_BYTES = 200_000


def clean_path(path: str) -> str:
    if (path or "").strip().startswith("/"):
        raise ProjectError(f"Unsafe file path {path!r}.")
    cleaned = (path or "").strip().replace("\\", "/")
    while "//" in cleaned:
        cleaned = cleaned.replace("//", "/")
    cleaned = cleaned.strip("/")
    if (not cleaned or len(cleaned) > MAX_PATH_CHARS
            or cleaned.startswith("/")
            or ".." in cleaned.split("/")
            or "\x00" in cleaned):
        raise ProjectError(f"Unsafe file path {path!r}.")
    return cleaned


def write_file(conn, project_id: int, path: str, content: str,
               updated_by: str = "") -> int:
    """Append a new version; returns the version number."""
    clean = clean_path(path)
    body = content or ""
    if len(body.encode("utf-8")) > MAX_FILE_BYTES:
        raise ProjectError(f"File {clean} exceeds the size limit.")
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS top FROM project_files"
        " WHERE project_id = ? AND path = ?",
        (project_id, clean)).fetchone()
    version = int(row["top"]) + 1
    insert_returning_id(
        conn,
        "INSERT INTO project_files (project_id, path, version, content,"
        " updated_by) VALUES (?, ?, ?, ?, ?)",
        (project_id, clean, version, body, (updated_by or "")[:120]))
    return version


def read_file(conn, project_id: int, path: str,
              version: int | None = None) -> dict | None:
    """Latest version (or a pinned one) as a dict, else None."""
    clean = clean_path(path)
    if version is None:
        row = conn.execute(
            "SELECT * FROM project_files WHERE project_id = ? AND path = ?"
            " ORDER BY version DESC LIMIT 1",
            (project_id, clean)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM project_files WHERE project_id = ? AND path = ?"
            " AND version = ?", (project_id, clean, version)).fetchone()
    return dict(row) if row is not None else None


def list_files(conn, project_id: int) -> list[dict]:
    """Latest version of every path, ordered by path."""
    rows = conn.execute(
        "SELECT f.* FROM project_files f"
        " JOIN (SELECT project_id, path, MAX(version) AS top"
        " FROM project_files GROUP BY project_id, path) latest"
        " ON latest.project_id = f.project_id"
        " AND latest.path = f.path AND latest.top = f.version"
        " WHERE f.project_id = ? ORDER BY f.path",
        (project_id,))
    return [dict(r) for r in rows]


def history(conn, project_id: int, path: str) -> list[dict]:
    """All versions of one path, oldest first."""
    clean = clean_path(path)
    rows = conn.execute(
        "SELECT version, updated_by, created_at FROM project_files"
        " WHERE project_id = ? AND path = ? ORDER BY version",
        (project_id, clean))
    return [dict(r) for r in rows]
