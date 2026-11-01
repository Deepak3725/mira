"""Seeker-request routes (compose, review, shortlist, contact).

Requests are private to their owner: only the owning user can view a request,
its shortlist, or contact providers from it. Contacting is idempotent --
repeated submissions reuse the open chat instead of duplicating it.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import auth
from .. import conversations as engine
from .. import requests as need_engine
from ..avatars import avatar_svg
from ..config import settings
from ..db import (
    get_connection,
    get_opportunity,
    get_twin_by_user,
    list_requests_for_user,
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


@router.get("/requests", response_class=HTMLResponse)
def request_list(request: Request):
    me, redir = _me_or_login(request)
    if redir:
        return redir
    conn = get_connection()
    try:
        rows = list_requests_for_user(conn, me["id"])
        items = [
            {"id": r["id"], "category": r["category"], "status": r["status"],
             "need_text": (r["need_text"] or "")[:120],
             "twin_name": r["twin_name"]}
            for r in rows
        ]
    finally:
        conn.close()
    return templates.TemplateResponse(
        request, "requests.html",
        {"app_name": settings.app_name, "me": me, "items": items})


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
            req_id, _ = need_engine.create_need(
                conn, twin_id=twin["id"], owner_user_id=me["id"],
                category=category if category in CATEGORIES else parsed["category"],
                need_text=need_text, requirements=parsed["requirements"],
                idempotency_key=idem_key)
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
         "csrf_token": auth.csrf_for_request(request, me["id"])})


def _requirements_from_form(budget: str, origin: str, destination: str,
                            when: str, skills: str) -> dict:
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
    return req


@router.post("/requests/{req_id}/confirm")
def request_confirm(
    request: Request, req_id: int,
    category: str = Form("other"),
    budget: str = Form(""), origin: str = Form(""), destination: str = Form(""),
    when: str = Form(""), skills: str = Form(""), csrf_token: str = Form(""),
):
    me, redir = _me_or_login(request)
    if redir:
        return redir
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not auth.check_rate_limit(f"reqwrite:{_ip(request)}", 60, 3600):
        return RedirectResponse(url="/requests", status_code=303)
    conn = get_connection()
    try:
        try:
            need_engine.confirm_request(
                conn, req_id, me["id"],
                category=category if category in CATEGORIES else "other",
                requirements=_requirements_from_form(budget, origin, destination,
                                                     when, skills))
        except need_engine.RequestError:
            raise HTTPException(status_code=404, detail="Request not found")
    finally:
        conn.close()
    return RedirectResponse(url=f"/requests/{req_id}", status_code=303)


@router.get("/requests/{req_id}", response_class=HTMLResponse)
def request_detail(request: Request, req_id: int):
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
        shortlist = find_providers(conn, req)
        if shortlist:
            need_engine.mark_matched(conn, req_id)
            req = need_engine.get_request(conn, req_id)
        for item in shortlist:
            seed = item["owner"].get("twin_avatar_seed")
            item["owner_svg"] = avatar_svg(seed or "mira-fallback")
        chats = [
            {"id": c["id"], "name_a": c["name_a"], "name_b": c["name_b"],
             "status": c["status"]}
            for c in conn.execute(
                "SELECT conversations.*, ta.display_name AS name_a,"
                " tb.display_name AS name_b FROM conversations"
                " JOIN twins ta ON ta.id = conversations.twin_a_id"
                " JOIN twins tb ON tb.id = conversations.twin_b_id"
                " WHERE conversations.request_id = ? ORDER BY conversations.id",
                (req_id,))
        ]
        view = {
            "id": req["id"], "category": req["category"], "status": req["status"],
            "need_text": req["need_text"], "twin_name": req["twin_name"],
            "requirements": need_engine.request_requirements(req),
        }
    finally:
        conn.close()
    return templates.TemplateResponse(
        request, "request_detail.html",
        {"app_name": settings.app_name, "me": me, "req": view,
         "shortlist": shortlist, "chats": chats,
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
                created_by_user_id=me["id"], request_id=req_id)
        except engine.ConversationError:
            return RedirectResponse(url=f"/requests/{req_id}", status_code=303)
    finally:
        conn.close()
    return RedirectResponse(url=f"/conversations/{conv_id}", status_code=303)


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
        except need_engine.RequestError:
            raise HTTPException(status_code=404, detail="Request not found")
    finally:
        conn.close()
    return RedirectResponse(url="/requests", status_code=303)
