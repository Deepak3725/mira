"""Twin interaction routes: Find Something (chat) + Post Something."""

from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import auth
from .. import findchat
from .. import posting
from ..config import settings
from ..db import (
    get_connection,
    get_opportunity_by_owner_title,
    get_twin_by_user,
    list_requests_for_user,
    list_twin_chats,
)
from .accounts import current_user

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["twin"])


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _me_or_login(request: Request):
    me = current_user(request)
    if me is None:
        return None, RedirectResponse(url="/login", status_code=303)
    return me, None


def _chat_cards(conn, messages: list) -> dict[int, list[dict]]:
    """Live match cards for each results message (ref request id)."""
    cards: dict[int, list[dict]] = {}
    for msg in messages:
        if msg["role"] == "twin" and msg["kind"] == "results" \
                and msg["ref_type"] == "request" and msg["ref_id"]:
            cards[msg["id"]] = findchat.match_cards(conn, msg["ref_id"])
    return cards


def _render_find(request: Request, me: dict, messages: list,
                 cards: dict, *, error: str = "",
                 draft: str = "", twin_name: str):
    return templates.TemplateResponse(
        request, "find.html",
        {"app_name": settings.app_name, "me": me,
         "messages": [dict(m) for m in messages], "cards": cards,
         "error": error, "draft": draft, "twin_name": twin_name,
         "csrf_token": auth.csrf_for_request(request, me["id"])},
        status_code=400 if error else 200)


@router.get("/find", response_class=HTMLResponse)
def find_page(request: Request):
    me, redir = _me_or_login(request)
    if redir:
        return redir
    conn = get_connection()
    try:
        twin = get_twin_by_user(conn, me["id"])
        if twin is None:
            return RedirectResponse(url="/twin/new", status_code=303)
        messages = list_twin_chats(conn, me["id"])
        cards = _chat_cards(conn, messages)
        return _render_find(request, me, messages, cards,
                            twin_name=twin["display_name"])
    finally:
        conn.close()


@router.post("/find")
def find_send(request: Request, message: str = Form(""),
              csrf_token: str = Form("")):
    me, redir = _me_or_login(request)
    if redir:
        return redir
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not auth.check_rate_limit(f"find:{_ip(request)}", 60, 3600):
        return RedirectResponse(url="/find", status_code=303)
    conn = get_connection()
    try:
        twin = get_twin_by_user(conn, me["id"])
        if twin is None:
            return RedirectResponse(url="/twin/new", status_code=303)
        if findchat.is_duplicate_submit(conn, me["id"], message):
            # Double-clicked Send: the identical search is already
            # being handled; show the conversation as-is.
            return RedirectResponse(url="/find", status_code=303)
        try:
            findchat.post_message(
                conn, user_id=me["id"], twin_id=twin["id"],
                text=message, twin_name=twin["display_name"])
        except (findchat.FindChatError, ValueError) as exc:
            messages = list_twin_chats(conn, me["id"])
            cards = _chat_cards(conn, messages)
            return _render_find(
                request, me, messages, cards, error=str(exc),
                draft=(message or "")[:2000],
                twin_name=twin["display_name"])
    finally:
        conn.close()
    return RedirectResponse(url="/find", status_code=303)


def _render_post(request: Request, me: dict, *, error: str = "",
                 draft: str = "", review: dict | None = None):
    return templates.TemplateResponse(
        request, "post.html",
        {"app_name": settings.app_name, "me": me, "error": error,
         "draft": draft, "review": review,
         "csrf_token": auth.csrf_for_request(request, me["id"])},
        status_code=400 if error else 200)


@router.get("/post", response_class=HTMLResponse)
def post_page(request: Request):
    me, redir = _me_or_login(request)
    if redir:
        return redir
    return _render_post(request, me)


@router.post("/post")
def post_submit(request: Request, description: str = Form(""),
                action: str = Form("draft"),
                title: str = Form(""), kind: str = Form(""),
                category: str = Form(""), skills: str = Form(""),
                location: str = Form(""), quantity: str = Form("1"),
                csrf_token: str = Form("")):
    me, redir = _me_or_login(request)
    if redir:
        return redir
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not auth.check_rate_limit(f"post:{_ip(request)}", 30, 3600):
        return RedirectResponse(url="/post", status_code=303)
    conn = get_connection()
    try:
        twin = get_twin_by_user(conn, me["id"])
        if twin is None:
            return RedirectResponse(url="/twin/new", status_code=303)
        if action == "publish":
            return _publish(request, me, conn, twin["id"], description,
                            title, kind, category, skills, location,
                            quantity)
        try:
            review = posting.parse_posting(description)
        except posting.PostingError as exc:
            return _render_post(request, me, error=str(exc),
                                draft=(description or "")[:4000])
        review["skills_text"] = ", ".join(review["skills"])
        return _render_post(request, me,
                            draft=(description or "")[:4000],
                            review=review)
    finally:
        conn.close()


def _publish(request: Request, me: dict, conn, twin_id: int,
             description: str, title: str, kind: str, category: str,
             skills: str, location: str, quantity: str):
    skill_list = [s.strip() for s in (skills or "").split(",")
                  if s.strip()][:20]
    try:
        qty = max(1, int((quantity or "1").strip()))
    except ValueError:
        qty = 1
    try:
        if (kind or "").strip().lower() == "offer":
            existing = get_opportunity_by_owner_title(
                conn, me["id"], (title or description or "").strip())
            if existing is not None and existing["status"] == "open" \
                    and (existing["description"] or "") == (description or ""):
                # Double-submitted publish: reuse the identical post.
                return RedirectResponse(
                    url=f"/opportunities/{existing['id']}", status_code=303)
            opp_id = posting.publish_offer(
                conn, owner_user_id=me["id"], title=title or description,
                description=description, skills=skill_list,
                location=location, category=category or "other")
            return RedirectResponse(url=f"/opportunities/{opp_id}",
                                    status_code=303)
        clean = (description or "").strip()
        for row in list_requests_for_user(conn, me["id"]):
            if row["need_text"] == clean and row["status"] not in (
                    "closed", "expired", "fulfilled"):
                # Double-submitted publish: reuse the identical search.
                return RedirectResponse(url=f"/requests/{row['id']}",
                                        status_code=303)
        req_id = posting.publish_need(
            conn, twin_id=twin_id, owner_user_id=me["id"],
            need_text=description, category=category or "other",
            skills=skill_list, location=location, quantity=qty)
        return RedirectResponse(url=f"/requests/{req_id}", status_code=303)
    except posting.PostingError as exc:
        review = {"kind": (kind or "need").strip().lower() or "need",
                  "title": (title or "")[:200],
                  "description": (description or "")[:4000],
                  "category": (category or "other").strip().lower(),
                  "skills": skill_list, "skills_text": skills or "",
                  "location": (location or "")[:120],
                  "quantity": qty}
        return _render_post(request, me, error=str(exc),
                            draft=(description or "")[:4000],
                            review=review)
