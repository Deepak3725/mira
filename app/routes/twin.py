"""Twin interaction routes: Find Something (chat) + Post Something."""

from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import auth
from .. import findchat
from .. import matcher
from .. import posting
from ..avatars import avatar_svg
from ..config import settings
from ..db import (
    add_twin_skills,
    get_connection,
    get_opportunity_by_owner_title,
    get_twin_by_user,
    list_requests_for_user,
    list_twin_chats,
    twin_profile,
)
from ..matching import find_jobs_by_title, find_other, find_taxi_rides
from .accounts import current_user

#: Structured search types on Find Something (GET query value).
SEARCH_TYPES = ("job", "taxi", "other")

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
                 draft: str = "", twin_name: str,
                 twin_skills: list | None = None,
                 notice: str = "", search: dict | None = None):
    return templates.TemplateResponse(
        request, "find.html",
        {"app_name": settings.app_name, "me": me,
         "messages": [dict(m) for m in messages if m["role"] != "system"], "cards": cards,
         "error": error, "draft": draft, "twin_name": twin_name,
         "twin_skills": twin_skills or [], "notice": notice,
         "search": search or {"type": "job", "did_search": False},
         "csrf_token": auth.csrf_for_request(request, me["id"])},
        status_code=400 if error else 200)


def _run_structured_search(conn, twin, search_type: str,
                           title: str, origin: str, destination: str,
                           query: str, location: str) -> dict:
    """One immediate structured search (read-only, no side effects).

    Searches real stored posts through the existing deterministic
    rules; never writes to the twin, never notifies, never invents.
    """
    title = (title or "").strip()[:200]
    origin = (origin or "").strip()[:120]
    destination = (destination or "").strip()[:120]
    query = (query or "").strip()[:500]
    location = (location or "").strip()[:120]
    base = {"type": search_type, "did_search": True, "results": [],
            "error": "", "title": title, "origin": origin,
            "destination": destination, "q": query, "location": location}
    if search_type == "job":
        if len(title) < 2:
            return dict(base, error="Enter a job title to search for.")
        return dict(base, results=find_jobs_by_title(
            conn, twin, title, location=location or None))
    if search_type == "taxi":
        if not origin and not destination:
            return dict(base, error="Enter where you are going from"
                                    " and/or going to.")
        return dict(base, results=find_taxi_rides(
            conn, twin, origin, destination))
    if len(query) < 2:
        return dict(base, error="Describe what you are looking for.")
    return dict(base, results=find_other(conn, twin, query))


def _twin_skill_list(conn, twin_id: int) -> list[str]:
    from ..db import get_twin

    twin = get_twin(conn, twin_id)
    if twin is None:
        return []
    skills = twin_profile(twin).get("skills", [])
    return [s for s in skills if isinstance(s, str) and s.strip()]


@router.get("/find", response_class=HTMLResponse)
def find_page(request: Request, notice: str = "",
              type: str = "", title: str = "", origin: str = "",
              destination: str = "", q: str = "", location: str = ""):
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
        search: dict | None = None
        picked = (type or "").strip().lower()
        if picked in SEARCH_TYPES:
            search = _run_structured_search(
                conn, twin, picked, title, origin, destination, q,
                location)
            for item in search.get("results", []):
                seed = (item.get("owner") or {}).get("twin_avatar_seed")
                item["owner_svg"] = avatar_svg(seed or "mira-fallback")
        elif type:
            search = {"type": "job", "did_search": False}
        return _render_find(
            request, me, messages, cards,
            twin_name=twin["display_name"],
            twin_skills=_twin_skill_list(conn, twin["id"]),
            notice=notice, search=search)
    finally:
        conn.close()


@router.post("/find/skills")
def find_add_skills(request: Request, skills: str = Form(""),
                    csrf_token: str = Form("")):
    """Add personal skills to the twin (single source of truth).

    New skills persist on the twin profile and apply to future job
    searches; duplicates are ignored.
    """
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
        new = findchat.parse_skill_list(skills)
        if not new:
            messages = list_twin_chats(conn, me["id"])
            cards = _chat_cards(conn, messages)
            return _render_find(
                request, me, messages, cards,
                error="Type at least one skill, e.g. Python, SQL.",
                twin_name=twin["display_name"],
                twin_skills=_twin_skill_list(conn, twin["id"]))
        stored = add_twin_skills(conn, twin["id"], new)
        conn.commit()
        from urllib.parse import quote as _quote

        notice = _quote(f"Skills saved: {', '.join(stored)}.")
    finally:
        conn.close()
    return RedirectResponse(url=f"/find?notice={notice}",
                            status_code=303)


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
                twin_name=twin["display_name"],
                twin_skills=_twin_skill_list(conn, twin["id"]))
    finally:
        conn.close()
    return RedirectResponse(url="/find", status_code=303)


def _render_post(request: Request, me: dict, *, error: str = "",
                 draft: str = "", review: dict | None = None):
    return templates.TemplateResponse(
        request, "post.html",
        {"app_name": settings.app_name, "me": me, "error": error,
         "draft": draft, "review": review,
         "durations": matcher.SEARCH_DURATIONS,
         "csrf_token": auth.csrf_for_request(request, me["id"])},
        status_code=400 if error else 200)


def _salary_number(value: str) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        number = float(value.strip().replace(",", ""))
    except ValueError:
        return None
    if number < 0:
        return None
    return number


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
                duration: str = Form("1w"),
                salary_min: str = Form(""), salary_max: str = Form(""),
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
                            quantity, duration, salary_min, salary_max)
        try:
            review = posting.parse_posting(description)
        except posting.PostingError as exc:
            return _render_post(request, me, error=str(exc),
                                draft=(description or "")[:4000])
        review["skills_text"] = ", ".join(review["skills"])
        if not review["location"] and review["category"] == "job":
            review["location"] = "Thimphu"
        review["duration"] = "1w"
        return _render_post(request, me,
                            draft=(description or "")[:4000],
                            review=review)
    finally:
        conn.close()


def _publish(request: Request, me: dict, conn, twin_id: int,
             description: str, title: str, kind: str, category: str,
             skills: str, location: str, quantity: str, duration: str,
             salary_min: str, salary_max: str):
    from urllib.parse import quote as _quote

    skill_list = [s.strip() for s in (skills or "").split(",")
                  if s.strip()][:20]
    try:
        qty = max(1, int((quantity or "1").strip()))
    except ValueError:
        qty = 1
    picked = duration if isinstance(duration, str) else "1w"
    picked = (picked.strip() or "1w")
    minutes = matcher.duration_minutes(picked, default=10080)
    expires_at = matcher.plus_minutes(
        matcher.now_stamp(), minutes) if minutes else None
    label = matcher.duration_label(picked)
    notice = _quote(f"Posted successfully — search duration: {label}.")
    low = _salary_number(salary_min)
    high = _salary_number(salary_max)
    if low is not None and high is not None and low > high:
        low, high = high, low
    try:
        if (kind or "").strip().lower() == "offer":
            existing = get_opportunity_by_owner_title(
                conn, me["id"], (title or description or "").strip())
            if existing is not None and existing["status"] == "open" \
                    and (existing["description"] or "") == (description or ""):
                # Double-submitted publish: reuse the identical post.
                return RedirectResponse(
                    url=f"/opportunities/{existing['id']}?notice={notice}",
                    status_code=303)
            opp_id = posting.publish_offer(
                conn, owner_user_id=me["id"], title=title or description,
                description=description, skills=skill_list,
                location=location, category=category or "other",
                salary_min=low, salary_max=high, expires_at=expires_at)
            return RedirectResponse(
                url=f"/opportunities/{opp_id}?notice={notice}",
                status_code=303)
        clean = (description or "").strip()
        for row in list_requests_for_user(conn, me["id"]):
            if row["need_text"] == clean and row["status"] not in (
                    "closed", "expired", "fulfilled"):
                # Double-submitted publish: reuse the identical search.
                return RedirectResponse(
                    url=f"/requests/{row['id']}?notice={notice}",
                    status_code=303)
        req_id = posting.publish_need(
            conn, twin_id=twin_id, owner_user_id=me["id"],
            need_text=description, category=category or "other",
            skills=skill_list, location=location, quantity=qty,
            expires_at=expires_at, salary_min=low, salary_max=high)
        return RedirectResponse(url=f"/requests/{req_id}?notice={notice}",
                                status_code=303)
    except posting.PostingError as exc:
        review = {"kind": (kind or "need").strip().lower() or "need",
                  "title": (title or "")[:200],
                  "description": (description or "")[:4000],
                  "category": (category or "other").strip().lower(),
                  "skills": skill_list, "skills_text": skills or "",
                  "location": (location or "")[:120],
                  "salary_min": salary_min or "", "salary_max": salary_max or "",
                  "duration": picked,
                  "quantity": qty}
        return _render_post(request, me, error=str(exc),
                            draft=(description or "")[:4000],
                            review=review)
