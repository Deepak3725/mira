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

#: Twin roles: the account registration no longer asks; the twin owns it.
TWIN_ROLES = ("job_seeker", "employer", "collaboration", "professional",
              "independent", "other")

SHAREABLE_FIELDS = (
    ("headline", "Headline"),
    ("role", "Role"),
    ("skills", "Skills"),
    ("experience_years", "Experience"),
    ("location", "Location"),
    ("availability", "Availability"),
    ("portfolio_url", "Portfolio link"),
    ("bio", "Bio"),
    ("interests", "Interests"),
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
        {"app_name": settings.app_name, "me": None, "error": "",
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
    # The registration form no longer asks for a role (it belongs to the
    # twin profile); the parameter stays so older clients/tests that
    # still send it keep working.
    auth.require_csrf(csrf_token, None)
    if not auth.check_rate_limit(f"register:{_ip(request)}", 10, 3600):
        return _form_error(request, "register.html", "Too many attempts. Try again later.")
    name, email = _clean(name, 60), _clean(email, 120).lower()
    if not name:
        return _form_error(request, "register.html", "Please enter a name.")
    if "@" not in email or "." not in email.split("@")[-1]:
        return _form_error(request, "register.html", "Please enter a valid email.")
    if len(password) < 8:
        return _form_error(request, "register.html",
                           "Password must be at least 8 characters.")
    if len(password) > auth.MAX_PASSWORD_LENGTH:
        return _form_error(request, "register.html",
                           "Password must be at most 128 characters.")
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
    if (settings.app_env == "production" and not settings.demo_mode
            and _clean(email, 120).lower().endswith("@example.com")):
        # Published demo credentials must never work on the public site,
        # unless this deployment is explicitly a demo environment.
        return _form_error(request, "login.html", "Invalid email or password.")
    conn = get_connection()
    try:
        row = get_user_by_email(conn, _clean(email, 120).lower())
    finally:
        conn.close()
    # Always run the password hash (dummy when unknown) so missing
    # accounts take the same time as wrong passwords.
    candidate = row["password_hash"] if row is not None else auth.DUMMY_HASH
    if row is None or not auth.verify_password(password, candidate):
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
    twin_role = get("role", "")
    if twin_role not in TWIN_ROLES:
        twin_role = ""
    return {
        "display_name": twin["display_name"] if twin else "",
        "persona": twin["persona"] if twin else "",
        "tone": twin["tone"] if twin else "",
        "twin_role": twin_role,
        "twin_roles": TWIN_ROLES,
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
        "interests": ", ".join(get("interests", []) or []),
        "requirements": ", ".join(get("requirements", []) or []),
        "phone": get("phone"), "personal_email": get("personal_email"),
        "open_to_remote": bool(get("open_to_remote", False)),
        # Private by default: new twins share nothing until the owner
        # ticks boxes; edits show the stored allowlist verbatim.
        "authorized": authorized,
        "fields": SHAREABLE_FIELDS,
    }


def _setup_page(request: Request, me: dict, state: dict, reply: str = ""):
    import json
    return templates.TemplateResponse(
        request, "twin_setup.html",
        {"app_name": settings.app_name, "me": me, "state": json.dumps(state),
         "phase": state.get("phase", "collect"), "reply": reply,
         "csrf_token": auth.csrf_for_request(request, me["id"])})


@router.get("/twin/setup", response_class=HTMLResponse)
def twin_setup_page(request: Request):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    conn = get_connection()
    try:
        if get_twin_by_user(conn, me["id"]) is not None:
            return RedirectResponse(url="/twin/edit", status_code=303)
    finally:
        conn.close()
    from ..twin_setup import new_setup, current_question
    state = new_setup()
    return _setup_page(request, me, state, current_question(state))


@router.post("/twin/setup", response_class=HTMLResponse)
def twin_setup_turn(request: Request, state: str = Form("{}"),
                    message: str = Form(""), csrf_token: str = Form("")):
    import json
    from .. import twin_setup
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    conn = get_connection()
    try:
        if get_twin_by_user(conn, me["id"]) is not None:
            return RedirectResponse(url="/twin/edit", status_code=303)
    finally:
        conn.close()
    try:
        draft = json.loads(state)
        if not isinstance(draft, dict) or not isinstance(draft.get("answers"), dict):
            raise ValueError
    except (ValueError, TypeError):
        draft = twin_setup.new_setup()
    command = (message or "").strip()
    if draft.get("phase") == "review" and command.lower() in (
            "continue to profile form", "continue", "open form"):
        answers = draft.get("answers", {})
        form = _twin_form_context()
        form.update({"display_name": answers.get("display_name", ""),
                     "headline": answers.get("headline", ""),
                     "twin_role": answers.get("role", "") if answers.get("role") in TWIN_ROLES else "",
                     "skills": ", ".join(answers.get("skills", [])),
                     "experience_years": answers.get("experience_years", ""),
                     "location": answers.get("location", ""),
                     "availability": answers.get("availability", ""),
                     "interests": ", ".join(answers.get("interests", [])),
                     "bio": answers.get("bio", ""),
                     "tone": answers.get("tone", "friendly")})
        return templates.TemplateResponse(
            request, "twin_form.html",
            {"app_name": settings.app_name, "me": me, "mode": "new",
             "form": form, "error": "",
             "notice": "Review these answers, then submit the form to create your private twin.",
             "csrf_token": auth.csrf_for_request(request, me["id"])})
    field = twin_setup.current_field(draft)
    if field == "role" and command.lower() not in ("skip", "skip it", "pass", "next", "none", "nothing", "n/a") and command.lower().replace(" ", "_") not in TWIN_ROLES:
        return _setup_page(request, me, draft, "Choose one listed role or say skip.")
    if field == "experience_years" and command.lower() not in ("skip", "skip it", "pass", "next", "none", "nothing", "n/a"):
        try:
            years = int(command)
            if not 0 <= years <= 80:
                raise ValueError
        except ValueError:
            return _setup_page(request, me, draft, "Enter a whole number from 0 to 80, or say skip.")
    result = twin_setup.setup_turn(draft, command)
    if result.get("cancelled"):
        return RedirectResponse(url="/twin/new", status_code=303)
    return _setup_page(request, me, draft, result.get("reply", ""))


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
    from ..privacy import twin_public_view

    return templates.TemplateResponse(
        request, "twin_form.html",
        {"app_name": settings.app_name, "me": me, "mode": "edit",
         "form": _twin_form_context(twin), "error": "",
         "preview": twin_public_view(twin),
         "csrf_token": auth.csrf_for_request(request, me["id"])},
    )


def _profile_from_form(
    headline: str, skills: str, experience_years: str, location: str,
    availability: str, portfolio_url: str, bio: str, company: str,
    role_hiring: str, requirements: str, phone: str, personal_email: str,
    open_to_remote: str | None, interests: str = "",
    twin_role: str = "",
) -> dict:
    def csv(value: object) -> list[str]:
        # Direct unit calls may leave a Form default in place of a
        # string; HTTP always supplies real strings.
        text = value if isinstance(value, str) else ""
        return [s.strip()[:120] for s in text.split(",") if s.strip()][:30]

    years: int | None = None
    try:
        years = max(0, min(80, int(experience_years.strip()))) if experience_years.strip() else None
    except ValueError:
        years = None
    # Direct (non-HTTP) callers may leave the Form sentinel in place.
    role = twin_role if isinstance(twin_role, str) else ""
    role = role.strip().lower()
    if role not in TWIN_ROLES:
        role = ""
    return {
        "headline": _clean(headline, 200), "role": role,
        "skills": csv(skills),
        "experience_years": years, "location": _clean(location, 120),
        "open_to_remote": bool(open_to_remote),
        "availability": _clean(availability, 200),
        "portfolio_url": _clean(portfolio_url, 300), "bio": _clean(bio, 1000),
        "interests": csv(interests),
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
    interests: str = Form(""), twin_role: str = Form(""),
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
                                 open_to_remote, interests, twin_role)
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
    interests: str = Form(""), twin_role: str = Form(""),
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
            from ..privacy import twin_public_view

            return _form_error(
                request, "twin_form.html", "Your twin needs a display name.",
                {"mode": "edit", "form": _twin_form_context(twin),
                 "preview": twin_public_view(twin)})
        allowed = [f for f in authorized if f in dict(SHAREABLE_FIELDS)]
        profile = _profile_from_form(headline, skills, experience_years, location,
                                     availability, portfolio_url, bio, company,
                                     role_hiring, requirements, phone, personal_email,
                                     open_to_remote, interests, twin_role)
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
