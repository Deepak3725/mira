"""Seeker-request routes (compose, review, providers, contact).

Requests are private to their owner: only the owning user can view a request,
its provider list, or contact providers from it. Contacting is idempotent --
repeated submissions reuse the open chat instead of duplicating it.
Provider lists show matching facts with no scores and no verdicts.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import auth
from .. import conversations as engine
from .. import matcher
from .. import requests as need_engine
from ..avatars import avatar_svg
from ..config import settings
from ..db import (
    get_connection,
    get_opportunity,
    get_twin_by_user,
    get_user,
    list_matches_for_peer,
    list_matches_for_search,
    list_notifications,
    list_requests_for_user,
    mark_all_notifications_read,
    mark_notification_read,
)
from ..intent import interpret_need
from ..matching import find_providers
from ..request_parse import CATEGORIES, parse_need
from .accounts import current_user

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["requests"])


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _me_or_login(request: Request):
    me = current_user(request)
    if me is None:
        return None, RedirectResponse(url="/login", status_code=303)
    return me, None


def _duration_minutes(value: str) -> int:
    for name, minutes in matcher.SEARCH_DURATIONS:
        if value == name:
            return minutes
    return 0


def _trigger_matching(conn, event_type: str, ref_type: str, ref_id: int) -> None:
    """Delegate to the shared matcher trigger (same behavior)."""
    matcher.trigger_matching(conn, event_type, ref_type, ref_id)


def _request_signature(req) -> str:
    import hashlib as _hashlib
    import json as _json

    blob = _json.dumps([req["need_text"], req["category"],
                        req["requirements_json"]], sort_keys=True)
    return _hashlib.sha256(blob.encode("utf-8")).hexdigest()


@router.get("/requests", response_class=HTMLResponse)
def request_list(request: Request):
    me, redir = _me_or_login(request)
    if redir:
        return redir
    conn = get_connection()
    try:
        rows = list_requests_for_user(conn, me["id"])
        items = []
        for r in rows:
            numbers = matcher.fulfillment(conn, r["id"])
            items.append({
                "id": r["id"], "category": r["category"],
                "status": r["status"],
                "title": need_engine.request_title(r["need_text"]),
                "need_text": (r["need_text"] or "")[:120],
                "twin_name": r["twin_name"],
                "expires_at": r["expires_at"],
                "fulfillment": numbers,
            })
        incoming = []
        for m in list_matches_for_peer(conn, me["id"]):
            if m["counterpart_type"] == "opportunity":
                opp = get_opportunity(conn, m["counterpart_id"])
                mine_title = opp["title"] if opp else "(removed offer)"
            else:
                other = need_engine.get_request(conn, m["counterpart_id"])
                mine_title = (need_engine.request_title(other["need_text"])
                              if other else "(removed need)")
            try:
                accept_message = m["accept_message"] or ""
            except (KeyError, IndexError, TypeError):
                accept_message = ""
            incoming.append({
                "id": m["id"], "status": m["status"],
                "owner_accepted": m["owner_accepted"],
                "peer_accepted": m["peer_accepted"],
                "mutual": matcher.is_mutual(m),
                "accept_message": accept_message,
                "contact": matcher.peer_contact_for_match(
                    conn, m, me["id"]),
                "reason": m["reason"],
                "mine_title": mine_title,
                "counterpart_type": m["counterpart_type"],
                "search_title": need_engine.request_title(m["search_text"]),
                "search_status": m["search_status"],
                "search_twin": m["search_twin"],
                "search_owner": m["search_owner"],
            })
        # Coordination both sides can see: every twin chat involving
        # my twin, with each owner's decision shown separately. Other
        # users' private request rows never appear here — only the
        # shared conversation both owners participate in.
        coordination = []
        for c in engine.list_for_user(conn, me["id"]):
            mine_a = c["owner_a_id"] == me["id"]
            coordination.append({
                "id": c["id"],
                "name_a": c["name_a"], "name_b": c["name_b"],
                "status": c["status"], "purpose": c["purpose"],
                "opp_title": c["opp_title"],
                "request_id": c["request_id"],
                "updated_at": c["updated_at"],
                "my_decision": (c["decision_a"] if mine_a
                                else c["decision_b"]),
                "peer_decision": (c["decision_b"] if mine_a
                                  else c["decision_a"]),
                "initiated_by_me": c["created_by_user_id"] == me["id"],
            })
    finally:
        conn.close()
    return templates.TemplateResponse(
        request, "requests.html",
        {"app_name": settings.app_name, "me": me, "items": items,
         "incoming": incoming, "coordination": coordination,
         "csrf_token": auth.csrf_for_request(request, me["id"])})


@router.get("/requests/new", response_class=HTMLResponse)
def request_new_form(request: Request):
    me, redir = _me_or_login(request)
    if redir:
        return redir
    conn = get_connection()
    try:
        twin = get_twin_by_user(conn, me["id"])
    finally:
        conn.close()
    if twin is None:
        return RedirectResponse(url="/twin/new", status_code=303)
    return templates.TemplateResponse(
        request, "request_new.html",
        {"app_name": settings.app_name, "me": me, "categories": CATEGORIES,
         "idem_key": uuid.uuid4().hex, "error": "",
         "csrf_token": auth.csrf_for_request(request, me["id"])})


@router.post("/requests/new")
def request_create(
    request: Request,
    need_text: str = Form(""),
    category: str = Form("other"),
    idem_key: str = Form(""),
    csrf_token: str = Form(""),
):
    me, redir = _me_or_login(request)
    if redir:
        return redir
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not auth.check_rate_limit(f"reqnew:{_ip(request)}", 30, 3600):
        return RedirectResponse(url="/requests", status_code=303)
    conn = get_connection()
    try:
        twin = get_twin_by_user(conn, me["id"])
        if twin is None:
            return RedirectResponse(url="/twin/new", status_code=303)
        parsed = parse_need(need_text)
        try:
            expires = None
            if parsed.get("deadline_minutes"):
                expires = matcher.plus_minutes(
                    matcher.now_stamp(), parsed["deadline_minutes"])
            req_id, _ = need_engine.create_need(
                conn, twin_id=twin["id"], owner_user_id=me["id"],
                category=category if category in CATEGORIES else parsed["category"],
                need_text=need_text, requirements=parsed["requirements"],
                idempotency_key=idem_key, expires_at=expires,
                quantity_requested=parsed.get("quantity") or 1)
        except need_engine.RequestError as exc:
            return templates.TemplateResponse(
                request, "request_new.html",
                {"app_name": settings.app_name, "me": me, "categories": CATEGORIES,
                 "idem_key": idem_key or uuid.uuid4().hex, "error": str(exc),
                 "csrf_token": auth.csrf_for_request(request, me["id"])},
                status_code=400)
    finally:
        conn.close()
    return RedirectResponse(url=f"/requests/{req_id}/review", status_code=303)


@router.get("/requests/{req_id}/review", response_class=HTMLResponse)
def request_review(request: Request, req_id: int):
    me, redir = _me_or_login(request)
    if redir:
        return redir
    conn = get_connection()
    try:
        req = need_engine.get_request(conn, req_id)
        if req is None or req["owner_user_id"] != me["id"]:
            raise HTTPException(status_code=404, detail="Request not found")
        if req["status"] != "draft":
            return RedirectResponse(url=f"/requests/{req_id}", status_code=303)
        parsed = parse_need(req["need_text"])
        current = need_engine.request_requirements(req)
        interp = interpret_need(req["need_text"], conn=conn, user_id=me["id"])
    finally:
        conn.close()
    return templates.TemplateResponse(
        request, "request_review.html",
        {"app_name": settings.app_name, "me": me, "req": dict(req),
         "current": current, "warnings": parsed["warnings"],
         "interp": interp, "categories": CATEGORIES, "error": "",
         "durations": matcher.SEARCH_DURATIONS,
         "picked_duration": ("none" if not req["expires_at"] else ""),
         "picked_quantity": req["quantity_requested"] or 1,
         "csrf_token": auth.csrf_for_request(request, me["id"])})


def _salary_or_none(value: str) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        number = float(value.strip().replace(",", ""))
    except ValueError:
        return None
    return number if number >= 0 else None


def _requirements_from_form(budget: str, origin: str, destination: str,
                            when: str, skills: str,
                            salary_min: str = "",
                            salary_max: str = "") -> dict:
    req: dict = {}
    budget = (budget or "").strip().replace(",", "")
    if budget:
        try:
            value = float(budget)
            if value >= 0:
                req["budget_max"] = value
        except ValueError:
            pass
    if origin.strip():
        req["origin"] = origin.strip()[:120]
    if destination.strip():
        req["destination"] = destination.strip()[:120]
    if when.strip():
        req["when_text"] = when.strip()[:120]
    parsed_skills = [s.strip()[:60] for s in skills.split(",") if s.strip()][:10]
    if parsed_skills:
        req["skills"] = parsed_skills
    low = _salary_or_none(salary_min)
    high = _salary_or_none(salary_max)
    if low is not None and high is not None and low > high:
        low, high = high, low
    if low is not None:
        req["salary_min"] = low
    if high is not None:
        req["salary_max"] = high
    return req


@router.post("/requests/{req_id}/confirm")
def request_confirm(
    request: Request, req_id: int,
    category: str = Form("other"),
    budget: str = Form(""), origin: str = Form(""), destination: str = Form(""),
    when: str = Form(""), skills: str = Form(""), csrf_token: str = Form(""),
    duration: str = Form("none"), quantity: str = Form(""),
    salary_min: str = Form(""), salary_max: str = Form(""),
):
    me, redir = _me_or_login(request)
    if redir:
        return redir
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not auth.check_rate_limit(f"reqwrite:{_ip(request)}", 60, 3600):
        return RedirectResponse(url="/requests", status_code=303)
    # Direct function calls (tests) pass Form() sentinels instead of
    # parsed values; normalize to the documented defaults.
    if not isinstance(duration, str):
        duration = "none"
    if not isinstance(quantity, str):
        quantity = ""
    conn = get_connection()
    try:
        try:
            # A duration picked here overrides any parsed deadline; "none"
            # keeps whatever the draft already holds (possibly none).
            # Blank quantity fields never arrive (urlencoded blanks are
            # dropped), so "" also means "keep the draft's value".
            minutes = _duration_minutes(duration)
            expires = (matcher.plus_minutes(matcher.now_stamp(), minutes)
                       if minutes else None)
            try:
                qty = int((quantity or "").strip()) or None
            except ValueError:
                qty = None
            need_engine.confirm_request(
                conn, req_id, me["id"],
                category=category if category in CATEGORIES else "other",
                requirements=_requirements_from_form(
                    budget, origin, destination, when, skills,
                    salary_min, salary_max),
                expires_at=expires, quantity_requested=qty)
        except need_engine.RequestError:
            raise HTTPException(status_code=404, detail="Request not found")
        # Immediate search of existing posts (event-driven; no polling).
        _trigger_matching(conn, "request_saved", "request", req_id)
    finally:
        conn.close()
    return RedirectResponse(url=f"/requests/{req_id}", status_code=303)


@router.get("/requests/{req_id}", response_class=HTMLResponse)
def request_detail(request: Request, req_id: int, notice: str = ""):
    me, redir = _me_or_login(request)
    if redir:
        return redir
    conn = get_connection()
    try:
        req = need_engine.get_request(conn, req_id)
        if req is None or req["owner_user_id"] != me["id"]:
            raise HTTPException(status_code=404, detail="Request not found")
        if req["status"] == "draft":
            return RedirectResponse(url=f"/requests/{req_id}/review", status_code=303)
        providers = find_providers(conn, req)
        if providers:
            need_engine.mark_matched(conn, req_id)
            req = need_engine.get_request(conn, req_id)
        for item in providers:
            seed = item["owner"].get("twin_avatar_seed")
            item["owner_svg"] = avatar_svg(seed or "mira-fallback")
        chats = [
            {"id": c["id"], "name_a": c["name_a"], "name_b": c["name_b"],
             "status": c["status"], "opp_title": c["opp_title"]}
            for c in conn.execute(
                "SELECT conversations.*, ta.display_name AS name_a,"
                " tb.display_name AS name_b, o.title AS opp_title"
                " FROM conversations"
                " JOIN twins ta ON ta.id = conversations.twin_a_id"
                " JOIN twins tb ON tb.id = conversations.twin_b_id"
                " LEFT JOIN opportunities o"
                " ON o.id = conversations.opportunity_id"
                " WHERE conversations.request_id = ? ORDER BY conversations.id",
                (req_id,))
        ]
        reqs = need_engine.request_requirements(req)
        numbers = matcher.fulfillment(conn, req["id"])
        match_items = []
        for m in list_matches_for_search(conn, req["id"]):
            peer_name, peer_twin, peer_link = "", "", ""
            if m["counterpart_type"] == "opportunity":
                opp = get_opportunity(conn, m["counterpart_id"])
                if opp is not None:
                    peer_name = opp["owner_name"]
                    peer_twin = opp["owner_twin_name"]
                    peer_link = f"/opportunities/{opp['id']}"
                    counter_title = opp["title"]
                else:
                    counter_title = "(removed offer)"
            else:
                other = need_engine.get_request(conn, m["counterpart_id"])
                if other is not None:
                    peer = get_user(conn, other["owner_user_id"])
                    peer_name = peer["name"] if peer else ""
                    peer_twin = other["twin_name"]
                    counter_title = need_engine.request_title(
                        other["need_text"])
                else:
                    counter_title = "(removed need)"
            try:
                accept_message = m["accept_message"] or ""
            except (KeyError, IndexError, TypeError):
                accept_message = ""
            match_items.append({
                "id": m["id"], "status": m["status"], "score": m["score"],
                "reason": m["reason"], "owner_accepted": m["owner_accepted"],
                "peer_accepted": m["peer_accepted"],
                "mutual": matcher.is_mutual(m),
                "accept_message": accept_message,
                "contact": matcher.peer_contact_for_match(
                    conn, m, me["id"]),
                "counterpart_type": m["counterpart_type"],
                "counter_title": counter_title, "peer_name": peer_name,
                "peer_twin": peer_twin, "peer_link": peer_link,
            })
        view = {
            "id": req["id"], "category": req["category"], "status": req["status"],
            "title": need_engine.request_title(req["need_text"]),
            "need_text": req["need_text"], "twin_name": req["twin_name"],
            "expires_at": req["expires_at"],
            "fulfillment": numbers,
            "requirements": reqs,
            "budget": reqs.get("budget_max", ""),
            "origin": reqs.get("origin", ""),
            "destination": reqs.get("destination", ""),
            "when": reqs.get("when_text", ""),
            "salary_min": reqs.get("salary_min", ""),
            "salary_max": reqs.get("salary_max", ""),
            "skills": ", ".join(reqs.get("skills", [])
                                if isinstance(reqs.get("skills"), list)
                                else []),
        }
    finally:
        conn.close()
    return templates.TemplateResponse(
        request, "request_detail.html",
        {"app_name": settings.app_name, "me": me, "req": view,
         "providers": providers, "chats": chats, "matches": match_items,
         "categories": CATEGORIES, "durations": matcher.SEARCH_DURATIONS,
         "notice": notice,
         "csrf_token": auth.csrf_for_request(request, me["id"])})


@router.post("/requests/{req_id}/contact")
def request_contact(request: Request, req_id: int, offering_id: int = Form(0),
                    csrf_token: str = Form("")):
    me, redir = _me_or_login(request)
    if redir:
        return redir
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not auth.check_rate_limit(f"contact:{_ip(request)}", 60, 3600):
        return RedirectResponse(url=f"/requests/{req_id}", status_code=303)
    conn = get_connection()
    try:
        req = need_engine.get_request(conn, req_id)
        if req is None or req["owner_user_id"] != me["id"]:
            raise HTTPException(status_code=404, detail="Request not found")
        if req["status"] == "draft":
            return RedirectResponse(url=f"/requests/{req_id}/review", status_code=303)
        opp = get_opportunity(conn, offering_id)
        if opp is None:
            raise HTTPException(status_code=404, detail="Offering not found")
        provider_twin = get_twin_by_user(conn, opp["owner_user_id"])
        if provider_twin is None:
            raise HTTPException(status_code=404, detail="Offering not found")
        try:
            conv_id, _ = engine.get_or_create_request_conversation(
                conn, twin_a_id=req["twin_id"], twin_b_id=provider_twin["id"],
                created_by_user_id=me["id"], request_id=req_id,
                opportunity_id=opp["id"])
        except engine.ConversationError:
            return RedirectResponse(url=f"/requests/{req_id}", status_code=303)
    finally:
        conn.close()
    return RedirectResponse(url=f"/conversations/{conv_id}", status_code=303)


@router.post("/requests/{req_id}/update")
def request_update(
    request: Request, req_id: int,
    need_text: str = Form(""),
    category: str = Form("other"),
    budget: str = Form(""), origin: str = Form(""), destination: str = Form(""),
    when: str = Form(""), skills: str = Form(""), csrf_token: str = Form(""),
    duration: str = Form("keep"), quantity: str = Form(""),
    salary_min: str = Form(""), salary_max: str = Form(""),
):
    """Owner edits a confirmed post (text, category, requirements)."""
    me, redir = _me_or_login(request)
    if redir:
        return redir
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not auth.check_rate_limit(f"reqwrite:{_ip(request)}", 60, 3600):
        return RedirectResponse(url=f"/requests/{req_id}", status_code=303)
    if not isinstance(duration, str):
        duration = "keep"
    if not isinstance(quantity, str):
        quantity = ""
    conn = get_connection()
    try:
        try:
            before = need_engine.get_request(conn, req_id)
            before_sig = _request_signature(before) if before else ""
            try:
                qty = int((quantity or "").strip()) if quantity.strip() else None
            except ValueError:
                qty = None
            expires: str | bool | None = None
            if duration == "none":
                expires = False  # clear the deadline
            elif _duration_minutes(duration):
                expires = matcher.plus_minutes(
                    matcher.now_stamp(), _duration_minutes(duration))
            need_engine.update_need(
                conn, req_id, me["id"], need_text=need_text,
                category=category if category in CATEGORIES else "other",
                requirements=_requirements_from_form(
                    budget, origin, destination, when, skills,
                    salary_min, salary_max),
                expires_at=expires, quantity_requested=qty)
            # Material edits re-trigger matching; no-op saves do not.
            after = need_engine.get_request(conn, req_id)
            if after is not None and _request_signature(after) != before_sig:
                _trigger_matching(conn, "request_saved", "request", req_id)
        except need_engine.RequestError:
            raise HTTPException(status_code=404, detail="Request not found")
    finally:
        conn.close()
    return RedirectResponse(url=f"/requests/{req_id}", status_code=303)


@router.post("/requests/{req_id}/close")
def request_close(request: Request, req_id: int, csrf_token: str = Form("")):
    me, redir = _me_or_login(request)
    if redir:
        return redir
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not auth.check_rate_limit(f"reqwrite:{_ip(request)}", 60, 3600):
        return RedirectResponse(url="/requests", status_code=303)
    conn = get_connection()
    try:
        try:
            need_engine.transition(conn, req_id, me["id"], "closed")
            matcher.withdraw_search_matches(conn, req_id)
        except need_engine.RequestError:
            raise HTTPException(status_code=404, detail="Request not found")
    finally:
        conn.close()
    return RedirectResponse(url="/requests", status_code=303)


@router.post("/requests/{req_id}/pause")
def request_pause(request: Request, req_id: int, csrf_token: str = Form("")):
    me, redir = _me_or_login(request)
    if redir:
        return redir
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not auth.check_rate_limit(f"reqwrite:{_ip(request)}", 60, 3600):
        return RedirectResponse(url=f"/requests/{req_id}", status_code=303)
    conn = get_connection()
    try:
        try:
            need_engine.transition(conn, req_id, me["id"], "paused")
        except need_engine.RequestError:
            raise HTTPException(status_code=404, detail="Request not found")
    finally:
        conn.close()
    return RedirectResponse(url=f"/requests/{req_id}", status_code=303)


@router.post("/requests/{req_id}/resume")
def request_resume(request: Request, req_id: int, csrf_token: str = Form("")):
    me, redir = _me_or_login(request)
    if redir:
        return redir
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not auth.check_rate_limit(f"reqwrite:{_ip(request)}", 60, 3600):
        return RedirectResponse(url=f"/requests/{req_id}", status_code=303)
    conn = get_connection()
    try:
        try:
            need_engine.transition(conn, req_id, me["id"], "open")
        except need_engine.RequestError:
            raise HTTPException(status_code=404, detail="Request not found")
        _trigger_matching(conn, "request_saved", "request", req_id)
    finally:
        conn.close()
    return RedirectResponse(url=f"/requests/{req_id}", status_code=303)


@router.post("/requests/{req_id}/extend")
def request_extend(request: Request, req_id: int,
                   duration: str = Form("1d"),
                   csrf_token: str = Form("")):
    me, redir = _me_or_login(request)
    if redir:
        return redir
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not auth.check_rate_limit(f"reqwrite:{_ip(request)}", 60, 3600):
        return RedirectResponse(url=f"/requests/{req_id}", status_code=303)
    minutes = _duration_minutes(duration) or 1440
    conn = get_connection()
    try:
        try:
            matcher.extend_search(conn, req_id, me["id"], minutes)
        except matcher.MatcherError:
            raise HTTPException(status_code=404, detail="Request not found")
        _trigger_matching(conn, "request_saved", "request", req_id)
    finally:
        conn.close()
    return RedirectResponse(url=f"/requests/{req_id}", status_code=303)


@router.post("/matches/{match_id}/accept")
def match_accept(request: Request, match_id: int,
                 message: str = Form(""),
                 csrf_token: str = Form("")):
    me, redir = _me_or_login(request)
    if redir:
        return redir
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not auth.check_rate_limit(f"reqwrite:{_ip(request)}", 60, 3600):
        return RedirectResponse(url="/requests", status_code=303)
    conn = get_connection()
    try:
        try:
            match = matcher.accept_match(conn, match_id, me["id"],
                                         message=message or "")
        except matcher.MatcherError:
            raise HTTPException(status_code=404, detail="Match not found")
        search = need_engine.get_request(conn, match["search_request_id"])
        mine = search is not None and search["owner_user_id"] == me["id"]
        back = (f"/requests/{match['search_request_id']}" if mine
                else "/requests")
    finally:
        conn.close()
    return RedirectResponse(url=back, status_code=303)


@router.post("/matches/{match_id}/reject")
def match_reject(request: Request, match_id: int,
                 csrf_token: str = Form("")):
    me, redir = _me_or_login(request)
    if redir:
        return redir
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not auth.check_rate_limit(f"reqwrite:{_ip(request)}", 60, 3600):
        return RedirectResponse(url="/requests", status_code=303)
    conn = get_connection()
    try:
        try:
            match = matcher.reject_match(conn, match_id, me["id"])
        except matcher.MatcherError:
            raise HTTPException(status_code=404, detail="Match not found")
        search = need_engine.get_request(conn, match["search_request_id"])
        mine = search is not None and search["owner_user_id"] == me["id"]
        back = (f"/requests/{match['search_request_id']}" if mine
                else "/requests")
    finally:
        conn.close()
    return RedirectResponse(url=back, status_code=303)


@router.get("/notifications", response_class=HTMLResponse)
def notification_list(request: Request):
    me, redir = _me_or_login(request)
    if redir:
        return redir
    conn = get_connection()
    try:
        items = [
            {"id": n["id"], "kind": n["kind"], "title": n["title"],
             "body": n["body"], "link": n["link"],
             "is_read": bool(n["is_read"]),
             "created_at": n["created_at"]}
            for n in list_notifications(conn, me["id"])
        ]
    finally:
        conn.close()
    return templates.TemplateResponse(
        request, "notifications.html",
        {"app_name": settings.app_name, "me": me, "items": items,
         "csrf_token": auth.csrf_for_request(request, me["id"])})


@router.post("/notifications/read")
def notification_read(request: Request, notif_id: str = Form(""),
                      csrf_token: str = Form("")):
    me, redir = _me_or_login(request)
    if redir:
        return redir
    auth.require_session_csrf(request, csrf_token, me["id"])
    target = "/notifications"
    conn = get_connection()
    try:
        if notif_id.strip() == "all":
            mark_all_notifications_read(conn, me["id"])
        elif notif_id.strip().isdigit():
            mark_notification_read(conn, int(notif_id), me["id"])
            # Reading one notification takes the user to what it is
            # about (the match view with Accept/Reject), when the link
            # is a safe same-site path.
            for note in list_notifications(conn, me["id"]):
                if note["id"] == int(notif_id) \
                        and (note["link"] or "").startswith("/"):
                    target = note["link"]
                    break
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url=target, status_code=303)
