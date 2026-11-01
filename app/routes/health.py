"""Health-check endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from ..config import settings
from ..db import db_path, get_connection, init_db

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict:
    """Report app status and database writability (never exposes secrets)."""
    try:
        init_db()
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        db_status = "ok" if row is not None else "empty"
    except Exception:
        return {
            "status": "degraded",
            "app": settings.app_name,
            "env": settings.app_env,
            "db": "error",
        }
    return {
        "status": "ok",
        "app": settings.app_name,
        "env": settings.app_env,
        "db": db_status,
    }
