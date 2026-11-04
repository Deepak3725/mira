"""Health-check endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from ..config import settings
from ..db import get_connection

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict:
    """Report app status with one cheap query (never exposes secrets).

    Schema setup runs once at boot (see lifespan in app.main); the
    check itself stays a single round trip so a slow database cannot
    push Render's 5-second health check into a timeout. An
    unreachable database still reports degraded, never ok.
    """
    try:
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
