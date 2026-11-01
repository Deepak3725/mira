"""Account, session, and twin-management routes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import auth, conversations as engine
from ..config import settings
from ..db import (
    DUPLICATE_ERRORS,
    create_twin,
    create_user,
    delete_twin,
    get_connection,
    get_twin_by_user,
    get_user,
    get_user_by_email,
    set_twin_conditions,
    update_twin,
)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["accounts"])

ROLES = ("job_seeker", "employer", "professional", "independent")

SHAREABLE_FIELDS = (
    ("headline", "Headline"),
    ("skills", "Skills"),
    ("experience_years", "Experience"),
    ("location", "Location"),
    ("availability", "Availability"),
    ("portfolio_url", "Portfolio link"),
    ("bio", "Bio"),
    ("company", "Company"),
    ("role_hiring", "Hiring role"),
    ("requirements", "Requirements"),
)


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def current_user(request: Request) -> dict[str, Any] | None:
    uid = auth.current_user_id(request)
    if uid is None:
        return None
    conn = get_connection()
    try:
        row = get_user(conn, uid)
    finally:
        conn.close()
    if row is None:
        return None
    return {"id": row["id"], "name": row["name"], "email": row["email"], "role": row["role"]}


def _clean(value: str | None, limit: int = 500) -> str:
    return (value or "").strip()[:limit]


def _login_response(user_id: int) -> RedirectResponse:
    resp = RedirectResponse(url="/me", status_code=303)
    resp.set_cookie(
        auth.SESSION_COOKIE, auth.create_session(user_id),
        httponly=True, samesite="lax", max_age=auth.SESSION_TTL_SECONDS,
        secure=settings.app_env == "production",
    )
    return resp


@router.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    me = current_user(request)
    if me:
        return RedirectResponse(url="/me", status_code=303)
    return templates.TemplateResponse(
        request, "register.html",
        {"app_name": settings.app_name, "me": None, "roles": ROLES, "error": "",
         "csrf_token": auth.csrf_token(None)},
    )


@router.post("/register")
def register(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    role: str = Form("professional"),
    csrf_token: str = Form(""),
):
    auth.require_csrf(csrf_token, None)
    if not auth.check_rate_limit(f"register:{_ip(request)}", 10, 3600):
        return _form_error(request, "register.html", "Too many attempts. Try again later.",
                           {"roles": ROLES})
    name, email = _clean(name, 60), _clean(email, 120).lower()
    if not name:
        return _form_error(request, "register.html", "Please enter a name.", {"roles": ROLES})
    if "@" not in email or "." not in email.split("@")[-1]:
        return _form_error(request, "register.html", "Please enter a valid email.", {"roles": ROLES})
    if len(password) < 8:
        return _form_error(request, "register.html",
                           "Password must be at least 8 characters.", {"roles": ROLES})
    if len(password) > auth.MAX_PASSWORD_LENGTH:
        return _form_error(request, "register.html",
                           "Password must be at most 128 characters.", {"roles": ROLES})
    if role not in ROLES:
        role = "professional"
    conn = get_connection()
    try:
        try:
            uid = create_user(conn, name=name, role=role, email=email,
                              password_hash=auth.hash_password(password))
            conn.commit()
        except DUPLICATE_ERRORS:
            conn.rollback()
            return _form_error(request, "register.html",
                               "That email is already registered.", {"roles": ROLES})
    finally:
        conn.close()
    return _login_response(uid)


def _conditions_from_form(max_price: str, auto_answer: str | None,
                          require_approval: str | None, distance: str,
                          availability: str) -> dict:
    conditions: dict = {
        "auto_answer": auto_answer is not None,
        "require_approval": require_approval is not None,
    }
    try:
        value = float((max_price or "").strip())
        if value >= 0:
            conditions["max_price"] = value
    except ValueError:
        pass
    try:
        dist = float((distance or "").strip())
        if dist >= 0:
            conditions["preferred_distance_km"] = dist
    except ValueError:
        pass
    if availability.strip():
        conditions["availability_note"] = _clean(availability, 200)
    return conditions


def _form_error(request: Request, template: str, error: str, extra: dict | None = None):
    me = current_user(request)
    if me is None:
        form_token = auth.csrf_token(None)
    else:
        form_token = auth.csrf_for_request(request, me["id"])
    ctx = {"app_name": settings.app_name, "me": me, "error": error,
           "csrf_token": form_token}
    if extra:
        ctx.update(extra)
    return templates.TemplateResponse(request, template, ctx, status_code=400)


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    me = current_user(request)
    if me:
        return RedirectResponse(url="/me", status_code=303)
    return templates.TemplateResponse(
        request, "login.html",
        {"app_name": settings.app_name, "me": None, "error": "",
         "csrf_token": auth.csrf_token(None)})


@router.post("/login")
def login(request: Request, email: str = Form(""), password: str = Form(""),
          csrf_token: str = Form("")):
    auth.require_csrf(csrf_token, None)
    if not auth.check_rate_limit(f"login:{_ip(request)}", 20, 3600):
        return _form_error(request, "login.html", "Too many attempts. Try again later.")
    if len(password) > auth.MAX_PASSWORD_LENGTH:
        return _form_error(request, "login.html", "Invalid email or password.")
    if settings.app_env == "production" and _clean(email, 120).lower().endswith("@example.com"):
        # Published demo credentials must never work on the public site.
        return _form_error(request, "login.html", "Invalid email or password.")
    conn = get_connection()
    try:
        row = get_user_by_email(conn, _clean(email, 120).lower())
    finally:
        conn.close()
    if row is None or not auth.verify_password(password, row["password_hash"]):
        return _form_error(request, "login.html", "Invalid email or password.")
    return _login_response(row["id"])


@router.post("/logout")
def logout(request: Request, csrf_token: str = Form("")):
    me = current_user(request)
    auth.require_session_csrf(request, csrf_token, me["id"] if me else None)
    session_value = request.cookies.get(auth.SESSION_COOKIE, "")
    if session_value:
        conn = get_connection()
        try:
            auth.revoke_session(conn, session_value)
        finally:
            conn.close()
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


@router.get("/me", response_class=HTMLResponse)
def my_space(request: Request):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    conn = get_connection()
    try:
        twin = get_twin_by_user(conn, me["id"])
        twin_view = None
        if twin:
            from ..privacy import twin_public_view

            twin_view = {
                "id": twin["id"], "display_name": twin["display_name"],
                "persona": twin["persona"], "tone": twin["tone"],
                "public": twin_public_view(twin),
            }
        convos = [
            {"id": c["id"], "name_a": c["name_a"], "name_b": c["name_b"],
             "status": c["status"], "purpose": c["purpose"], "opp_title": c["opp_title"]}
            for c in engine.list_for_user(conn, me["id"])
        ]
        approved_memories = 0
        if twin:
            approved_memories = conn.execute(
                "SELECT COUNT(*) AS n FROM memories WHERE twin_id = ? AND status = 'approved'",
                (twin["id"],),
            ).fetchone()["n"]
    finally:
        conn.close()
    return templates.TemplateResponse(
        request, "me.html",
        {"app_name": settings.app_name, "me": me, "twin": twin_view,
         "convos": convos, "approved_memories": approved_memories,
         "csrf_token": auth.csrf_for_request(request, me["id"])})


def _twin_form_context(twin=None) -> dict:
    import json as _json

    profile = {}
    authorized: list = []
    if twin is not None:
        try:
            profile = _json.loads(twin["profile_json"] or "{}")
        except (ValueError, TypeError):
            profile = {}
        try:
            authorized = _json.loads(twin["authorized_fields_json"] or "[]")
        except (ValueError, TypeError):
            authorized = []
    if not isinstance(profile, dict):
        profile = {}
    conditions = {}
    if twin is not None:
        try:
            conditions = _json.loads(twin["conditions_json"] or "{}")
        except (ValueError, TypeError):
            conditions = {}
    if not isinstance(conditions, dict):
        conditions = {}
    get = lambda k, d="": profile.get(k, d)  # noqa: E731
    return {
        "display_name": twin["display_name"] if twin else "",
        "persona": twin["persona"] if twin else "",
        "tone": twin["tone"] if twin else "",
        "is_public": bool(twin["is_public"]) if twin else False,
        "cond_max_price": conditions.get("max_price", ""),
        "cond_auto_answer": conditions.get("auto_answer", True),
        "cond_require_approval": conditions.get("require_approval", False),
        "cond_distance": conditions.get("preferred_distance_km", ""),
        "cond_availability": conditions.get("availability_note", ""),
        "headline": get("headline"), "skills": ", ".join(get("skills", []) or []),
        "experience_years": get("experience_years", ""),
        "location": get("location"), "availability": get("availability"),
        "portfolio_url": get("portfolio_url"), "bio": get("bio"),
        "company": get("company"), "role_hiring": get("role_hiring"),
        "requirements": ", ".join(get("requirements", []) or []),
        "phone": get("phone"), "personal_email": get("personal_email"),
        "open_to_remote": bool(get("open_to_remote", False)),
        "authorized": authorized or [f for f, _ in SHAREABLE_FIELDS[:7]],
        "fields": SHAREABLE_FIELDS,
    }


@router.get("/twin/new", response_class=HTMLResponse)
def twin_new_form(request: Request):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    conn = get_connection()
    try:
        if get_twin_by_user(conn, me["id"]) is not None:
            return RedirectResponse(url="/twin/edit", status_code=303)
    finally:
        conn.close()
    return templates.TemplateResponse(
        request, "twin_form.html",
        {"app_name": settings.app_name, "me": me, "mode": "new",
         "form": _twin_form_context(), "error": "",
         "csrf_token": auth.csrf_for_request(request, me["id"])},
    )


@router.get("/twin/edit", response_class=HTMLResponse)
def twin_edit_form(request: Request):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    conn = get_connection()
    try:
        twin = get_twin_by_user(conn, me["id"])
    finally:
        conn.close()
    if twin is None:
        return RedirectResponse(url="/twin/new", status_code=303)
    return templates.TemplateResponse(
        request, "twin_form.html",
        {"app_name": settings.app_name, "me": me, "mode": "edit",
         "form": _twin_form_context(twin), "error": "",
         "csrf_token": auth.csrf_for_request(request, me["id"])},
    )


def _profile_from_form(
    headline: str, skills: str, experience_years: str, location: str,
    availability: str, portfolio_url: str, bio: str, company: str,
    role_hiring: str, requirements: str, phone: str, personal_email: str,
    open_to_remote: str | None,
) -> dict:
    def csv(value: str) -> list[str]:
        return [s.strip()[:120] for s in value.split(",") if s.strip()][:30]

    years: int | None = None
    try:
        years = max(0, min(80, int(experience_years.strip()))) if experience_years.strip() else None
    except ValueError:
        years = None
    return {
        "headline": _clean(headline, 200), "skills": csv(skills),
        "experience_years": years, "location": _clean(location, 120),
        "open_to_remote": bool(open_to_remote),
        "availability": _clean(availability, 200),
        "portfolio_url": _clean(portfolio_url, 300), "bio": _clean(bio, 1000),
        "company": _clean(company, 200), "role_hiring": _clean(role_hiring, 200),
        "requirements": csv(requirements),
        "phone": _clean(phone, 60), "personal_email": _clean(personal_email, 120),
    }


@router.post("/twin/new")
def twin_create(
    request: Request,
    display_name: str = Form(""), persona: str = Form(""), tone: str = Form(""),
    headline: str = Form(""), skills: str = Form(""), experience_years: str = Form(""),
    location: str = Form(""), availability: str = Form(""), portfolio_url: str = Form(""),
    bio: str = Form(""), company: str = Form(""), role_hiring: str = Form(""),
    requirements: str = Form(""), phone: str = Form(""), personal_email: str = Form(""),
    open_to_remote: str | None = Form(None), authorized: list[str] = Form([]),
    public_listing: str | None = Form(None),
    cond_max_price: str = Form(""), cond_auto_answer: str | None = Form(None),
    cond_require_approval: str | None = Form(None), cond_distance: str = Form(""),
    cond_availability: str = Form(""), csrf_token: str = Form(""),
):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not auth.check_rate_limit(f"twinwrite:{_ip(request)}", 60, 3600):
        return RedirectResponse(url="/me", status_code=303)
    display_name = _clean(display_name, 60)
    if not display_name:
        return _form_error(request, "twin_form.html", "Your twin needs a display name.",
                           {"mode": "new", "form": _twin_form_context()})
    allowed = [f for f in authorized if f in dict(SHAREABLE_FIELDS)]
    profile = _profile_from_form(headline, skills, experience_years, location,
                                 availability, portfolio_url, bio, company,
                                 role_hiring, requirements, phone, personal_email,
                                 open_to_remote)
    conn = get_connection()
    try:
        if get_twin_by_user(conn, me["id"]) is not None:
            return RedirectResponse(url="/twin/edit", status_code=303)
        try:
            twin_id = create_twin(conn, user_id=me["id"], display_name=display_name,
                        persona=_clean(persona, 600), tone=_clean(tone, 120) or "friendly",
                        profile=profile, authorized_fields=allowed,
                        avatar_seed=f"user-{me['id']}-twin",
                        is_public=bool(public_listing))
            set_twin_conditions(
                conn, twin_id,
                _conditions_from_form(cond_max_price, cond_auto_answer,
                                      cond_require_approval, cond_distance,
                                      cond_availability))
            conn.commit()
        except DUPLICATE_ERRORS:
            conn.rollback()
            return _form_error(request, "twin_form.html",
                               "You already have a twin.", {"mode": "new", "form": _twin_form_context()})
    finally:
        conn.close()
    return RedirectResponse(url="/me", status_code=303)


@router.post("/twin/edit")
def twin_update(
    request: Request,
    display_name: str = Form(""), persona: str = Form(""), tone: str = Form(""),
    headline: str = Form(""), skills: str = Form(""), experience_years: str = Form(""),
    location: str = Form(""), availability: str = Form(""), portfolio_url: str = Form(""),
    bio: str = Form(""), company: str = Form(""), role_hiring: str = Form(""),
    requirements: str = Form(""), phone: str = Form(""), personal_email: str = Form(""),
    open_to_remote: str | None = Form(None), authorized: list[str] = Form([]),
    public_listing: str | None = Form(None),
    cond_max_price: str = Form(""), cond_auto_answer: str | None = Form(None),
    cond_require_approval: str | None = Form(None), cond_distance: str = Form(""),
    cond_availability: str = Form(""), csrf_token: str = Form(""),
):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not auth.check_rate_limit(f"twinwrite:{_ip(request)}", 60, 3600):
        return RedirectResponse(url="/me", status_code=303)
    display_name = _clean(display_name, 60)
    conn = get_connection()
    try:
        twin = get_twin_by_user(conn, me["id"])
        if twin is None:
            return RedirectResponse(url="/twin/new", status_code=303)
        if not display_name:
            return _form_error(request, "twin_form.html", "Your twin needs a display name.",
                               {"mode": "edit", "form": _twin_form_context(twin)})
        allowed = [f for f in authorized if f in dict(SHAREABLE_FIELDS)]
        profile = _profile_from_form(headline, skills, experience_years, location,
                                     availability, portfolio_url, bio, company,
                                     role_hiring, requirements, phone, personal_email,
                                     open_to_remote)
        update_twin(conn, twin["id"], display_name=display_name,
                    persona=_clean(persona, 600), tone=_clean(tone, 120) or "friendly",
                    profile=profile, authorized_fields=allowed,
                    is_public=bool(public_listing))
        set_twin_conditions(
            conn, twin["id"],
            _conditions_from_form(cond_max_price, cond_auto_answer,
                                  cond_require_approval, cond_distance,
                                  cond_availability))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/me", status_code=303)


@router.post("/twin/delete")
def twin_delete(request: Request, confirm: str = Form(""), csrf_token: str = Form("")):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not auth.check_rate_limit(f"twinwrite:{_ip(request)}", 60, 3600):
        return RedirectResponse(url="/me", status_code=303)
    if confirm.strip().lower() != "delete":
        return RedirectResponse(url="/twin/edit", status_code=303)
    conn = get_connection()
    try:
        twin = get_twin_by_user(conn, me["id"])
        if twin is not None:
            delete_twin(conn, twin["id"])
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/me", status_code=303)
