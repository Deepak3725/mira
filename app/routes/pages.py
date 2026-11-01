"""HTML page routes (Jinja2)."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import auth
from ..avatars import STATES, avatar_svg
from ..config import settings
from ..db import (
    create_opportunity,
    get_connection,
    get_opportunity,
    get_twin,
    get_twin_by_user,
    list_twins,
    visible_twins,
)
from ..request_parse import CATEGORIES as REQUEST_CATEGORIES
from ..matching import SeekerProfile, find_matches, score_opportunity
from ..privacy import twin_public_view
from .accounts import current_user

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["pages"])


def _owner_svg(seed: str | None) -> str:
    return avatar_svg(seed or "mira-fallback")


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


OPP_KINDS = ("job", "service", "supply", "collaboration", "other")


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "index.html",
        {"app_name": settings.app_name, "me": current_user(request)},
    )


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    from ..auth import current_user_id

    viewer = current_user_id(request)
    conn = get_connection()
    try:
        rows = visible_twins(conn, viewer)
        twins = [
            {
                "display_name": row["display_name"],
                "owner_name": row["owner_name"],
                "owner_role": row["owner_role"],
                "persona": row["persona"],
                "tone": row["tone"],
                "avatar_svg": avatar_svg(row["avatar_seed"]),
                "public": twin_public_view(row),
            }
            for row in rows
        ]
        spotlights = []
        for row in rows:
            top = find_matches(conn, row, limit=2)
            for item in top:
                item["owner_svg"] = _owner_svg(item["owner"]["twin_avatar_seed"])
            spotlights.append(
                {
                    "twin_id": row["id"],
                    "display_name": row["display_name"],
                    "owner_name": row["owner_name"],
                    "matches": top,
                }
            )
    finally:
        conn.close()
    mood_seed = rows[0]["avatar_seed"] if rows else "mira-demo"
    moods = [{"state": s, "svg": avatar_svg(mood_seed, s)} for s in STATES]
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "app_name": settings.app_name,
            "me": current_user(request),
            "twins": twins,
            "moods": moods,
            "spotlights": spotlights,
        },
    )


@router.get("/discover", response_class=HTMLResponse)
def discover(
    request: Request,
    twin_id: int | None = None,
    skills: str = "",
    location: str = "",
) -> HTMLResponse:
    from ..auth import current_user_id

    viewer = current_user_id(request)
    conn = get_connection()
    try:
        twins = visible_twins(conn, viewer)
        selected = next((t for t in twins if t["id"] == twin_id), None)
        if selected is None and twins:
            selected = twins[0]
        skills_filter = [s.strip() for s in skills.split(",") if s.strip()]
        matches: list = []
        if selected is not None:
            matches = find_matches(
                conn,
                selected,
                skills_filter=skills_filter or None,
                location_filter=location.strip() or None,
            )
            for item in matches:
                item["owner_svg"] = _owner_svg(item["owner"]["twin_avatar_seed"])
        picker = [
            {"id": t["id"], "display_name": t["display_name"], "owner_name": t["owner_name"]}
            for t in twins
        ]
        sel = (
            {"id": selected["id"], "display_name": selected["display_name"]}
            if selected
            else None
        )
    finally:
        conn.close()
    return templates.TemplateResponse(
        request,
        "discover.html",
        {
            "app_name": settings.app_name,
            "me": current_user(request),
            "twins": picker,
            "selected": sel,
            "skills_query": skills,
            "location_query": location,
            "matches": matches,
        },
    )


@router.get("/opportunities/new", response_class=HTMLResponse)
def opportunity_new_form(request: Request, error: str = ""):
    from .accounts import current_user

    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(
        request, "opportunity_form.html",
        {"app_name": settings.app_name, "me": me, "error": error,
         "kinds": OPP_KINDS, "categories": REQUEST_CATEGORIES,
         "csrf_token": auth.csrf_for_request(request, me["id"])})


@router.post("/opportunities/new")
def opportunity_create(
    request: Request,
    kind: str = Form("job"),
    title: str = Form(""),
    description: str = Form(""),
    skills: str = Form(""),
    years: str = Form(""),
    location: str = Form(""),
    remote_ok: str = Form(""),
    category: str = Form(""),
    csrf_token: str = Form(""),
):
    from .accounts import current_user

    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not auth.check_rate_limit(f"oppnew:{_ip(request)}", 30, 3600):
        return RedirectResponse(url="/discover", status_code=303)
    clean_title = (title or "").strip()
    clean_kind = (kind or "").strip().lower()
    if not clean_title:
        return RedirectResponse(url="/opportunities/new?error=need-title",
                                status_code=303)
    if clean_kind not in OPP_KINDS:
        return RedirectResponse(url="/opportunities/new?error=bad-kind",
                                status_code=303)
    clean_category = (category or "").strip().lower()
    if clean_category and clean_category not in REQUEST_CATEGORIES:
        return RedirectResponse(url="/opportunities/new?error=bad-category",
                                status_code=303)
    try:
        wanted_years = int(years.strip()) if years.strip() else None
    except ValueError:
        wanted_years = None
    conn = get_connection()
    try:
        opp_id = create_opportunity(
            conn, owner_user_id=me["id"], kind=clean_kind,
            title=clean_title[:200], description=(description or "")[:4000],
            required_skills=[s.strip()[:60] for s in skills.split(",")
                             if s.strip()][:20],
            preferred_experience_years=wanted_years,
            location=(location or "").strip()[:120],
            remote_ok=remote_ok.strip().lower() in ("yes", "on", "true",
                                                    "1"),
            category=clean_category)
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url=f"/opportunities/{opp_id}", status_code=303)


@router.get("/opportunities/{opp_id}", response_class=HTMLResponse)
def opportunity_detail(
    request: Request, opp_id: int, twin_id: int | None = None
) -> HTMLResponse:
    conn = get_connection()
    try:
        opp = get_opportunity(conn, opp_id)
        if opp is None:
            raise HTTPException(status_code=404, detail="Opportunity not found")
        twin = get_twin(conn, twin_id) if twin_id is not None else None
        match = None
        selected = None
        if twin is not None:
            # Public detail page: match from the authorized view only.
            seeker = SeekerProfile.from_profile(twin_public_view(twin))
            try:
                required = json.loads(opp["required_skills_json"] or "[]")
            except (ValueError, TypeError):
                required = []
            match = score_opportunity(
                seeker,
                required_skills=required if isinstance(required, list) else [],
                preferred_years=opp["preferred_experience_years"],
                location=opp["location"],
                remote_ok=bool(opp["remote_ok"]),
            )
            selected = {"id": twin["id"], "display_name": twin["display_name"]}
        view = {
            "id": opp["id"],
            "kind": opp["kind"],
            "title": opp["title"],
            "description": opp["description"],
            "required_skills": json.loads(opp["required_skills_json"] or "[]"),
            "preferred_experience_years": opp["preferred_experience_years"],
            "location": opp["location"],
            "remote_ok": bool(opp["remote_ok"]),
            "status": opp["status"],
        }
        owner = {
            "name": opp["owner_name"],
            "role": opp["owner_role"],
            "twin_display_name": opp["owner_twin_name"],
        }
        owner_svg = _owner_svg(opp["owner_avatar_seed"])
        me = current_user(request)
        my_twin = owner_twin = None
        if me is not None:
            my_twin = get_twin_by_user(conn, me["id"])
            owner_twin = get_twin_by_user(conn, opp["owner_user_id"])
        can_contact = bool(
            me and my_twin and owner_twin and my_twin["id"] != owner_twin["id"]
            and owner_twin["is_public"]
        )
    finally:
        conn.close()
    return templates.TemplateResponse(
        request,
        "opportunity.html",
        {
            "app_name": settings.app_name,
            "me": me,
            "opp": view,
            "owner": owner,
            "owner_svg": owner_svg,
            "match": match,
            "selected": selected,
            "my_twin": (
                {"id": my_twin["id"], "display_name": my_twin["display_name"]}
                if my_twin else None
            ),
            "owner_twin_id": owner_twin["id"] if owner_twin else None,
            "can_contact": can_contact,
            "csrf_token": auth.csrf_for_request(
                request, me["id"] if me else None),
        },
    )
