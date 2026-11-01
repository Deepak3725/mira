"""MIRA FastAPI application entrypoint (Phase 0 skeleton)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import init_db
from .log import configure_stderr, get_logger
from .routes import (
    accounts,
    avatars,
    channels,
    chats,
    health,
    memories,
    pages,
    projects,
    requests,
    voice,
)

BASE_DIR = Path(__file__).resolve().parent

DEV_SESSION_SECRET = "dev-only-insecure-secret-change-me"


def check_production_settings() -> None:
    """Refuse to boot publicly with development secrets."""
    if settings.app_env != "production":
        return
    if settings.session_secret in ("", DEV_SESSION_SECRET):
        raise RuntimeError("SESSION_SECRET must be set in production.")
    if len(settings.session_secret) < 32:
        raise RuntimeError("SESSION_SECRET must be at least 32 characters.")
    if not (settings.database_url or "").strip():
        raise RuntimeError("DATABASE_URL must be set in production.")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    check_production_settings()
    configure_stderr()
    init_db()
    yield


def _docs_kwargs() -> dict:
    """API docs stay available in dev, hidden on the public site."""
    if settings.app_env == "production":
        return {"docs_url": None, "redoc_url": None, "openapi_url": None}
    return {}


app = FastAPI(
    title=settings.app_name,
    description="Personal AI digital-twin network (MVP skeleton).",
    lifespan=lifespan,
    **_docs_kwargs(),
)

#: Largest accepted request body (covers the 5 MB import + form overhead).
#: Chunked bodies without Content-Length bypass this; parsers cap anyway.
MAX_BODY_BYTES = 6 * 1024 * 1024


async def limit_body_size(request: Request, call_next):
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_BODY_BYTES:
        return HTMLResponse("Request body too large.", status_code=413)
    return await call_next(request)


async def log_requests(request: Request, call_next):
    import time
    import uuid

    start = time.monotonic()
    request_id = uuid.uuid4().hex[:12]
    response = await call_next(request)
    duration_ms = int((time.monotonic() - start) * 1000)
    get_logger().info(
        "request",
        extra={"request_id": request_id, "method": request.method,
               "path": request.url.path, "status": response.status_code,
               "duration_ms": duration_ms},
    )
    response.headers["X-Request-ID"] = request_id
    return response


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    return response


app.middleware("http")(limit_body_size)
app.middleware("http")(log_requests)
app.middleware("http")(security_headers)


def _error_page(status: int, title: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><title>{status} — {title}</title></head>"
        f"<body><h1>{status} — {title}</h1>"
        f"<p><a href=\"/\">Back to {settings.app_name}</a></p></body></html>",
        status_code=status,
    )


@app.exception_handler(HTTPException)
async def _http_errors(request: Request, exc: HTTPException) -> HTMLResponse:
    if exc.status_code == 404:
        return _error_page(404, "Not found")
    if exc.status_code == 403:
        return _error_page(403, "Invalid form token. Go back and try again.")
    return _error_page(exc.status_code, "Something went wrong")


@app.exception_handler(Exception)
async def _uncaught_errors(request: Request, exc: Exception) -> HTMLResponse:
    # Never leak tracebacks or internals to the browser.
    return _error_page(500, "Something went wrong")

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(health.router)
app.include_router(avatars.router)
app.include_router(accounts.router)
app.include_router(chats.router)
app.include_router(memories.router)
app.include_router(requests.router)
app.include_router(pages.router)
app.include_router(projects.router)
app.include_router(channels.router)
app.include_router(voice.router)
