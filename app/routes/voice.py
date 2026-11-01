"""Voice routes: talk (or type) to your twin.

GET /voice renders the voice page (login required). The page uses
the browser's Web Speech API when available and always offers a
typed box. POST /voice/command accepts one transcript and returns
a JSON reply; JavaScript speaks the ``speak`` field aloud.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import auth
from ..channels.types import ChannelError
from ..config import settings
from ..db import get_connection, get_twin_by_user
from ..projects import ProjectError
from ..voice import available, handle_transcript
from ..voice.types import VoiceError
from .accounts import current_user

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["voice"])


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _limited(request: Request) -> bool:
    return auth.check_rate_limit(f"voice:{_ip(request)}", 120, 3600)


@router.get("/voice", response_class=HTMLResponse)
def voice_page(request: Request):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(
        request, "voice.html",
        {"app_name": settings.app_name, "me": me,
         "providers": available(),
         "csrf_token": auth.csrf_for_request(request, me["id"])},
    )


@router.post("/voice/command")
def voice_command(request: Request, transcript: str = Form(""),
                  provider: str = Form("browser"),
                  csrf_token: str = Form("")):
    me = current_user(request)
    if me is None:
        return JSONResponse({"error": "Login required."},
                            status_code=401)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not _limited(request):
        return JSONResponse(
            {"error": "Too many voice commands; try again later."},
            status_code=429)
    conn = get_connection()
    try:
        twin = get_twin_by_user(conn, me["id"])
        try:
            reply = handle_transcript(
                conn, user_id=me["id"],
                twin_id=twin["id"] if twin else None,
                text=transcript, provider=provider)
        except (VoiceError, ChannelError, ProjectError) as exc:
            conn.rollback()
            return JSONResponse({"error": str(exc)[:500]},
                                status_code=400)
        conn.commit()
    finally:
        conn.close()
    return JSONResponse(reply)
