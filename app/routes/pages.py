"""HTML page routes (Jinja2)."""

from __future__ import annotations

import json
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
    create_opportunity,
    get_connection,
    get_opportunity,
    get_twin_by_user,
    update_opportunity,
)
from ..request_parse import CATEGORIES as REQUEST_CATEGORIES
from ..matching import SeekerProfile, find_matches, find_needs, score_opportunity
from ..privacy import twin_public_view
from .accounts import current_user

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["pages"])


def _owner_svg(seed: str | None) -> str:
    return avatar_svg(seed or "mira-fallback")


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _trigger_offer_matching(conn, opp_id: int) -> None:
    """Delegate to the shared matcher trigger (same behavior)."""
    matcher.trigger_matching(conn, "opportunity_saved", "opportunity",
                             opp_id)


def _opp_signature(opp) -> str:
    import hashlib as _hashlib
    import json as _json

    blob = _json.dumps([opp["title"], opp["description"],
                        opp["required_skills_json"], opp["location"],
                        opp["remote_ok"], opp["category"],
                        opp["attributes_json"]], sort_keys=True)
    return _hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _unscored_listings(conn, viewer: int | None,
                       skills_filter: list[str],
                       location: str) -> list:
    """Public opportunity listings without relevance scores.

    For visitors and owners without a twin: same skill/location
    filters as scored discovery, but no twin is scored.
    """
    from ..db import list_opportunities

    wanted = {s.strip().lower() for s in skills_filter if s.strip()}
    loc = (location or "").strip().lower()
    out = []
    for opp in list_opportunities(conn, status="open"):
        try:
            required = json.loads(opp["required_skills_json"] or "[]")
        except (ValueError, TypeError):
            required = []
        if not isinstance(required, list):
            required = []
        if wanted and not (wanted & {str(s).lower() for s in required}):
            continue
        if loc:
            if loc == "remote":
                if not opp["remote_ok"]:
                    continue
            elif loc not in (opp["location"] or "").lower():
                continue
        out.append({
            "opportunity": {
                "id": opp["id"], "kind": opp["kind"],
                "title": opp["title"],
                "description": opp["description"],
                "required_skills": [s for s in required
                                    if isinstance(s, str)],
                "preferred_experience_years":
                    opp["preferred_experience_years"],
                "location": opp["location"],
                "remote_ok": bool(opp["remote_ok"]),
                "status": opp["status"],
            },
            "owner": {
                "name": opp["owner_name"],
                "role": opp["owner_role"],
                "twin_display_name": opp["owner_twin_name"],
                "twin_avatar_seed": opp["owner_avatar_seed"],
            },
            "match": None,
            "owner_svg": _owner_svg(opp["owner_avatar_seed"]),
        })
    return out


OPP_KINDS = ("job", "service", "supply", "collaboration", "other")


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    me = current_user(request)
    if me is not None:
        return RedirectResponse(url="/find", status_code=303)
    return templates.TemplateResponse(
        request, "index.html",
        {"app_name": settings.app_name, "me": None},
    )


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    from ..auth import current_user_id

    viewer = current_user_id(request)
    conn = get_connection()
    try:
        own = get_twin_by_user(conn, viewer) if viewer else None
        own_view = None
        if own is not None:
            public = twin_public_view(own)
            own_view = {
                "display_name": own["display_name"],
                "persona": own["persona"],
                "tone": own["tone"],
                "avatar_svg": avatar_svg(own["avatar_seed"]),
                "public": public,
            }
    finally:
        conn.close()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "app_name": settings.app_name,
            "me": current_user(request),
            "own": own_view,
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

    # NOTE (hackathon compliance): relevance is computed for the
    # viewer's OWN twin only. Scoring other people's twins against
    # jobs is applicant screening and is not offered: the legacy
    # twin_id parameter is honored only when it names the viewer's
    # own twin, otherwise ignored. Visitors without a twin see
    # unscored public listings.
    viewer = current_user_id(request)
    conn = get_connection()
    try:
        own = get_twin_by_user(conn, viewer) if viewer else None
        if twin_id is not None and (own is None or twin_id != own["id"]):
            twin_id = None
        selected = own
        skills_filter = [s.strip() for s in skills.split(",") if s.strip()]
        matches: list = []
        needs: list = []
        if selected is not None:
            matches = find_matches(
                conn,
                selected,
                skills_filter=skills_filter or None,
                location_filter=location.strip() or None,
            )
            for item in matches:
                item["owner_svg"] = _owner_svg(item["owner"]["twin_avatar_seed"])
            # Reverse discovery: other owners' shared needs this twin
            # could help with. Same page, same twin, other direction.
            needs = find_needs(conn, selected)
            for item in needs:
                item["seeker_svg"] = _owner_svg(
                    item["seeker"]["twin_avatar_seed"])
        else:
            matches = _unscored_listings(
                conn, viewer, skills_filter, location.strip())
        picker = (
            [{"id": own["id"], "display_name": own["display_name"],
              "owner_name": ""}]
            if own else []
        )
        sel = (
            {"id": selected["id"], "display_name": selected["display_name"]}
            if selected
            else None
        )
    finally:
        conn.close()
    me = current_user(request)
    return templates.TemplateResponse(
        request,
        "discover.html",
        {
            "app_name": settings.app_name,
            "me": me,
            "twins": picker,
            "selected": sel,
            "skills_query": skills,
            "location_query": location,
            "matches": matches,
            "needs": needs,
            "csrf_token": (auth.csrf_for_request(request, me["id"])
                           if me else ""),
        },
    )


@router.post("/discover/needs/{req_id}/contact")
def need_contact(request: Request, req_id: int, csrf_token: str = Form("")):
    """Answer a discovered need: my twin contacts the seeker's twin.

    Mirror of the seeker-side /requests/{id}/contact. All rules are
    enforced server-side: login, my twin exists, the need is shared
    (not private/withdrawn), and never my own need.
    """
    from ..db import get_request

    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not auth.check_rate_limit(f"contact:{_ip(request)}", 60, 3600):
        return RedirectResponse(url="/discover", status_code=303)
    conn = get_connection()
    try:
        mine = get_twin_by_user(conn, me["id"])
        req = get_request(conn, req_id)
        if mine is None or req is None:
            raise HTTPException(status_code=404, detail="Not found")
        if req["owner_user_id"] == me["id"]:
            raise HTTPException(status_code=404, detail="Not found")
        if not need_engine.is_shared(req):
            raise HTTPException(status_code=404, detail="Not found")
        seeker_twin = get_twin_by_user(conn, req["owner_user_id"])
        if seeker_twin is None:
            raise HTTPException(status_code=404, detail="Not found")
        try:
            conv_id, _ = engine.get_or_create_request_conversation(
                conn, twin_a_id=mine["id"], twin_b_id=seeker_twin["id"],
                created_by_user_id=me["id"], request_id=req_id)
        except engine.ConversationError:
            return RedirectResponse(url="/discover", status_code=303)
    finally:
        conn.close()
    return RedirectResponse(url=f"/conversations/{conv_id}", status_code=303)


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
        # Saving a post triggers matching promptly (no polling).
        _trigger_offer_matching(conn, opp_id)
    finally:
        conn.close()
    return RedirectResponse(url=f"/opportunities/{opp_id}", status_code=303)


@router.post("/opportunities/{opp_id}/edit")
def opportunity_edit(
    request: Request, opp_id: int,
    title: str = Form(""), description: str = Form(""),
    skills: str = Form(""), years: str = Form(""),
    location: str = Form(""), remote_ok: str = Form(""),
    category: str = Form(""), duration: str = Form("keep"),
    salary_min: str = Form(""), salary_max: str = Form(""),
    csrf_token: str = Form(""),
):
    from .accounts import current_user

    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not auth.check_rate_limit(f"oppnew:{_ip(request)}", 30, 3600):
        return RedirectResponse(url="/discover", status_code=303)
    conn = get_connection()
    try:
        opp = get_opportunity(conn, opp_id)
        if opp is None or opp["owner_user_id"] != me["id"]:
            raise HTTPException(status_code=404, detail="Not found")
        before = _opp_signature(opp)
        clean_category = (category or "").strip().lower()
        if clean_category and clean_category not in REQUEST_CATEGORIES:
            return RedirectResponse(
                url=f"/opportunities/{opp_id}", status_code=303)
        try:
            wanted_years = int(years.strip()) if years.strip() else None
        except ValueError:
            wanted_years = None
        try:
            attributes = json.loads(opp["attributes_json"] or "{}")
        except (ValueError, TypeError):
            attributes = {}
        if not isinstance(attributes, dict):
            attributes = {}
        for key, raw in (("salary_min", salary_min),
                         ("salary_max", salary_max)):
            raw = raw if isinstance(raw, str) else ""
            raw = raw.strip().replace(",", "")
            if not raw:
                attributes.pop(key, None)
                continue
            try:
                value = float(raw)
            except ValueError:
                continue
            if value >= 0:
                attributes[key] = value
        fields: dict = {
            "title": (title or "").strip()[:200] or opp["title"],
            "description": (description or "")[:4000],
            "required_skills": [s.strip()[:60] for s in skills.split(",")
                                if s.strip()][:20],
            "preferred_experience_years": wanted_years,
            "location": (location or "").strip()[:120],
            "remote_ok": remote_ok.strip().lower() in ("yes", "on", "true",
                                                       "1"),
            "category": clean_category,
            "attributes": attributes,
        }
        picked = duration if isinstance(duration, str) else "keep"
        picked = picked.strip()
        if picked == "none":
            fields["expires_at"] = None
        elif matcher.duration_minutes(picked, default=0):
            fields["expires_at"] = matcher.plus_minutes(
                matcher.now_stamp(),
                matcher.duration_minutes(picked))
        update_opportunity(conn, opp_id, **fields)
        conn.commit()
        after = get_opportunity(conn, opp_id)
        if after is not None and _opp_signature(after) != before:
            _trigger_offer_matching(conn, opp_id)
    finally:
        conn.close()
    return RedirectResponse(url=f"/opportunities/{opp_id}", status_code=303)


@router.post("/opportunities/{opp_id}/close")
def opportunity_close(request: Request, opp_id: int,
                      csrf_token: str = Form("")):
    from .accounts import current_user

    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    conn = get_connection()
    try:
        opp = get_opportunity(conn, opp_id)
        if opp is None or opp["owner_user_id"] != me["id"]:
            raise HTTPException(status_code=404, detail="Not found")
        update_opportunity(conn, opp_id, status="closed")
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url=f"/opportunities/{opp_id}", status_code=303)


@router.post("/opportunities/{opp_id}/reopen")
def opportunity_reopen(request: Request, opp_id: int,
                       csrf_token: str = Form("")):
    from .accounts import current_user

    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    conn = get_connection()
    try:
        opp = get_opportunity(conn, opp_id)
        if opp is None or opp["owner_user_id"] != me["id"]:
            raise HTTPException(status_code=404, detail="Not found")
        # Reopening clears any elapsed search duration; the owner can set
        # a fresh one from the edit form.
        update_opportunity(conn, opp_id, status="open", expires_at=None)
        conn.commit()
        _trigger_offer_matching(conn, opp_id)
    finally:
        conn.close()
    return RedirectResponse(url=f"/opportunities/{opp_id}", status_code=303)


@router.get("/opportunities/{opp_id}", response_class=HTMLResponse)
def opportunity_detail(
    request: Request, opp_id: int, twin_id: int | None = None,
    notice: str = "",
) -> HTMLResponse:
    conn = get_connection()
    try:
        opp = get_opportunity(conn, opp_id)
        if opp is None:
            raise HTTPException(status_code=404, detail="Opportunity not found")
        from ..auth import current_user_id as _viewer

        viewer = _viewer(request)
        own = get_twin_by_user(conn, viewer) if viewer else None
        # NOTE (hackathon compliance): the relevance block scores the
        # viewer's OWN twin only; a twin_id naming anyone else is
        # ignored instead of scoring them.
        twin = own if (own is not None and twin_id == own["id"]) else None
        match = None
        selected = None
        # One guarded parse for every use below: corrupt or non-list
        # stored skills degrade to "no specific skills" instead of a
        # 500 on this notification-click target.
        try:
            required_skills = json.loads(
                opp["required_skills_json"] or "[]")
        except (ValueError, TypeError):
            required_skills = []
        if not isinstance(required_skills, list):
            required_skills = []
        if twin is not None:
            # Relevance from the authorized view only, own twin only.
            seeker = SeekerProfile.from_profile(twin_public_view(twin))
            match = score_opportunity(
                seeker,
                required_skills=required_skills,
                preferred_years=opp["preferred_experience_years"],
                location=opp["location"],
                remote_ok=bool(opp["remote_ok"]),
            )
            selected = {"id": twin["id"], "display_name": twin["display_name"]}
        try:
            attributes = json.loads(opp["attributes_json"] or "{}")
        except (ValueError, TypeError):
            attributes = {}
        if not isinstance(attributes, dict):
            attributes = {}
        try:
            expires_at = opp["expires_at"]
        except (KeyError, IndexError):
            expires_at = None
        view = {
            "id": opp["id"],
            "kind": opp["kind"],
            "title": opp["title"],
            "description": opp["description"],
            "required_skills": required_skills,
            "preferred_experience_years": opp["preferred_experience_years"],
            "location": opp["location"],
            "remote_ok": bool(opp["remote_ok"]),
            "status": opp["status"],
            "expires_at": expires_at,
            "salary_min": attributes.get("salary_min", ""),
            "salary_max": attributes.get("salary_max", ""),
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
            "is_owner": bool(me and me["id"] == opp["owner_user_id"]),
            "categories": REQUEST_CATEGORIES,
            "opp_category": opp["category"] or "",
            "durations": matcher.SEARCH_DURATIONS,
            "notice": notice,
            "skills_text": ", ".join(
                s for s in required_skills if isinstance(s, str)),
            "csrf_token": auth.csrf_for_request(
                request, me["id"] if me else None),
        },
    )
