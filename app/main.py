"""MIRA FastAPI application entrypoint (Phase 0 skeleton)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from starlette.exceptions import HTTPException as StarletteHTTPException
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
    twin,
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


def _sweep_forever(stop, interval: float) -> None:
    """Deadline + match-queue sweeper. Matches never wait for this —
    post-save processing is inline; the sweep only expires searches
    and retries failed events. One worker per deployment."""
    from .db import get_connection
    from .matcher import now_stamp, process_due

    log = get_logger()
    while not stop.wait(interval):
        conn = None
        try:
            conn = get_connection()
            counts = process_due(conn, now_stamp())
            if counts["expired"] or counts["failed"] or counts["matches"]:
                log.info("sweep", extra=counts)
        except Exception as exc:  # noqa: BLE001 - sweeper never dies
            log.warning("sweep failed", extra={"error": str(exc)[:200]})
        finally:
            if conn is not None:
                conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    check_production_settings()
    configure_stderr()
    import os as _os
    import threading as _threading

    from .db import test_mode_active as _test_mode

    if not _test_mode() and _os.environ.get(
            "MIRA_TEST_DATABASE_URL", "").strip().lower() not in ("", "sqlite"):
        # Never print the value (it may hold credentials): the redirect
        # is test-only, so the server ignores it and says so once.
        get_logger().warning(
            "ignoring test-only MIRA_TEST_DATABASE_URL outside test mode")
    init_db()
    stop = _threading.Event()
    worker = None
    if settings.worker_enabled:
        worker = _threading.Thread(
            target=_sweep_forever,
            args=(stop, max(5, settings.worker_interval_seconds)),
            name="mira-sweeper", daemon=True)
        worker.start()
    try:
        yield
    finally:
        stop.set()
        if worker is not None:
            worker.join(timeout=5)


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
    # Styled like the app without needing request context; never leaks
    # tracebacks or internals.
    return HTMLResponse(
        "<!doctype html><html lang=\"en\"><head>"
        "<meta charset=\"utf-8\" />"
        "<meta name=\"viewport\" content=\"width=device-width,"
        " initial-scale=1\" />"
        f"<title>{status} — {title}</title>"
        "<link rel=\"stylesheet\" href=\"/static/css/style.css\" />"
        "</head><body>"
        "<header class=\"site-header\"><div class=\"container"
        " header-inner\">"
        f"<a class=\"brand\" href=\"/\">{settings.app_name}</a>"
        "</div></header>"
        "<main class=\"container\">"
        f"<h1>{status} — {title}</h1>"
        f"<p class=\"lede\"><a href=\"/\">Back to"
        f" {settings.app_name}</a></p>"
        "</main></body></html>",
        status_code=status,
    )


# NOTE: fastapi.HTTPException and starlette.HTTPException are distinct
# classes in this stack, and router 404s raise the starlette one. The
# handler is registered for both so error pages always render as HTML.
@app.exception_handler(HTTPException)
@app.exception_handler(StarletteHTTPException)
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
app.include_router(twin.router)
