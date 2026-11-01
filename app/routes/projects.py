"""Project execution routes (owner-only)."""

from __future__ import annotations

import shlex
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import auth
from ..config import settings
from ..db import get_connection
from ..projects import engine, list_files, write_file
from ..projects.graph import ProjectError
from ..projects.spec import clarify_questions, draft_spec
from .accounts import current_user

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["projects"])


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _limited(request: Request) -> bool:
    return auth.check_rate_limit(f"projwrite:{_ip(request)}", 120, 3600)


def _own_project_or_404(conn, project_id: int, me: dict):
    project = engine.get_project(conn, project_id, me["id"])
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _ctx(request: Request, me: dict, project, notice="", error=""):
    conn = get_connection()
    try:
        tasks = [dict(t) for t in engine.list_tasks(conn, project["id"])]
        files = list_files(conn, project["id"])
        events = engine.recent_events(conn, project["id"], 20)
    finally:
        conn.close()
    return {"app_name": settings.app_name, "me": me,
            "project": dict(project), "tasks": tasks, "files": files,
            "events": events, "notice": notice, "error": error,
            "questions": clarify_questions(project["goal"]),
            "kinds": engine.TASK_KINDS,
            "csrf_token": auth.csrf_for_request(request, me["id"])}


@router.get("/projects", response_class=HTMLResponse)
def project_list(request: Request):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    conn = get_connection()
    try:
        projects = [dict(p) for p in engine.list_projects(conn, me["id"])]
    finally:
        conn.close()
    return templates.TemplateResponse(
        request, "projects.html",
        {"app_name": settings.app_name, "me": me,
         "projects": projects,
         "csrf_token": auth.csrf_for_request(request, me["id"])})


@router.post("/projects/new")
def project_create(request: Request, title: str = Form(""),
                   goal: str = Form(""),
                   csrf_token: str = Form("")):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not _limited(request):
        return RedirectResponse(url="/projects", status_code=303)
    conn = get_connection()
    try:
        try:
            pid = engine.create_project(conn, me["id"], title, goal)
            conn.commit()
        except ProjectError as exc:
            conn.rollback()
            return RedirectResponse(url="/projects", status_code=303)
    finally:
        conn.close()
    return RedirectResponse(url=f"/projects/{pid}", status_code=303)


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def project_detail(request: Request, project_id: int, notice: str = "",
                   error: str = ""):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    conn = get_connection()
    try:
        project = _own_project_or_404(conn, project_id, me)
    finally:
        conn.close()
    return templates.TemplateResponse(
        request, "project.html", _ctx(request, me, project, notice, error))


@router.post("/projects/{project_id}/spec")
def project_spec(request: Request, project_id: int,
                 deliverable: str = Form(""),
                 audience: str = Form(""),
                 csrf_token: str = Form("")):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not _limited(request):
        return RedirectResponse(url=f"/projects/{project_id}",
                                status_code=303)
    conn = get_connection()
    try:
        project = _own_project_or_404(conn, project_id, me)
        answers = {}
        if deliverable.strip():
            answers["deliverable"] = deliverable.strip()[:120]
        if audience.strip():
            answers["audience"] = audience.strip()[:120]
        try:
            engine.save_spec(conn, project_id, me["id"],
                             draft_spec(project["goal"], answers))
            conn.commit()
        except ProjectError as exc:
            conn.rollback()
            return RedirectResponse(
                url=f"/projects/{project_id}?error={str(exc)[:120]}",
                status_code=303)
    finally:
        conn.close()
    return RedirectResponse(
        url=f"/projects/{project_id}?notice=spec-saved", status_code=303)


@router.post("/projects/{project_id}/tasks")
def project_add_task(request: Request, project_id: int,
                     kind: str = Form("chat"), title: str = Form(""),
                     instructions: str = Form(""),
                     csrf_token: str = Form("")):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not _limited(request):
        return RedirectResponse(url=f"/projects/{project_id}",
                                status_code=303)
    conn = get_connection()
    try:
        _own_project_or_404(conn, project_id, me)
        try:
            engine.add_task(conn, project_id, me["id"], kind=kind,
                            title=title, instructions=instructions)
            conn.commit()
        except ProjectError as exc:
            conn.rollback()
            return RedirectResponse(
                url=f"/projects/{project_id}?error={str(exc)[:120]}",
                status_code=303)
    finally:
        conn.close()
    return RedirectResponse(
        url=f"/projects/{project_id}?notice=task-added", status_code=303)


@router.post("/projects/{project_id}/advance")
def project_advance(request: Request, project_id: int,
                    csrf_token: str = Form("")):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not _limited(request):
        return RedirectResponse(url=f"/projects/{project_id}",
                                status_code=303)
    conn = get_connection()
    try:
        _own_project_or_404(conn, project_id, me)
        try:
            outcomes = engine.advance(conn, project_id, me["id"],
                                      user_id=me["id"], max_tasks=1)
            conn.commit()
        except ProjectError as exc:
            conn.rollback()
            return RedirectResponse(
                url=f"/projects/{project_id}?error={str(exc)[:120]}",
                status_code=303)
    finally:
        conn.close()
    if not outcomes:
        notice = "nothing-ready"
    else:
        first = outcomes[0]
        notice = f"task-{first['id']}-{first['status']}"
    return RedirectResponse(url=f"/projects/{project_id}?notice={notice}",
                            status_code=303)


@router.post("/projects/{project_id}/tasks/{task_id}/approve")
def project_approve_task(request: Request, project_id: int, task_id: int,
                         csrf_token: str = Form("")):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not _limited(request):
        return RedirectResponse(url=f"/projects/{project_id}",
                                status_code=303)
    conn = get_connection()
    try:
        _own_project_or_404(conn, project_id, me)
        try:
            engine.approve_task(conn, project_id, task_id, me["id"])
            conn.commit()
        except ProjectError as exc:
            conn.rollback()
            return RedirectResponse(
                url=f"/projects/{project_id}?error={str(exc)[:120]}",
                status_code=303)
    finally:
        conn.close()
    return RedirectResponse(
        url=f"/projects/{project_id}?notice=task-approved", status_code=303)


@router.post("/projects/{project_id}/tasks/{task_id}/retry")
def project_retry_task(request: Request, project_id: int, task_id: int,
                       csrf_token: str = Form("")):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not _limited(request):
        return RedirectResponse(url=f"/projects/{project_id}",
                                status_code=303)
    conn = get_connection()
    try:
        _own_project_or_404(conn, project_id, me)
        try:
            engine.retry_task(conn, project_id, task_id, me["id"])
            conn.commit()
        except ProjectError as exc:
            conn.rollback()
            return RedirectResponse(
                url=f"/projects/{project_id}?error={str(exc)[:120]}",
                status_code=303)
    finally:
        conn.close()
    return RedirectResponse(
        url=f"/projects/{project_id}?notice=task-retried", status_code=303)


@router.post("/projects/{project_id}/tasks/{task_id}/cancel")
def project_cancel_task(request: Request, project_id: int, task_id: int,
                        csrf_token: str = Form("")):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not _limited(request):
        return RedirectResponse(url=f"/projects/{project_id}",
                                status_code=303)
    conn = get_connection()
    try:
        _own_project_or_404(conn, project_id, me)
        try:
            engine.cancel_task(conn, project_id, task_id, me["id"])
            conn.commit()
        except ProjectError as exc:
            conn.rollback()
            return RedirectResponse(
                url=f"/projects/{project_id}?error={str(exc)[:120]}",
                status_code=303)
    finally:
        conn.close()
    return RedirectResponse(
        url=f"/projects/{project_id}?notice=task-cancelled", status_code=303)


@router.post("/projects/{project_id}/tasks/{task_id}/review")
def project_review_task(request: Request, project_id: int, task_id: int,
                        note: str = Form(""),
                        csrf_token: str = Form("")):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not _limited(request):
        return RedirectResponse(url=f"/projects/{project_id}",
                                status_code=303)
    conn = get_connection()
    try:
        _own_project_or_404(conn, project_id, me)
        try:
            engine.request_review(conn, project_id, task_id, me["id"],
                                  note=note)
            conn.commit()
        except ProjectError as exc:
            conn.rollback()
            return RedirectResponse(
                url=f"/projects/{project_id}?error={str(exc)[:120]}",
                status_code=303)
    finally:
        conn.close()
    return RedirectResponse(
        url=f"/projects/{project_id}?notice=review-requested",
        status_code=303)


@router.post("/projects/{project_id}/status")
def project_status(request: Request, project_id: int,
                   action: str = Form(""),
                   csrf_token: str = Form("")):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not _limited(request):
        return RedirectResponse(url=f"/projects/{project_id}",
                                status_code=303)
    mapping = {"pause": "paused", "resume": "active", "cancel": "cancelled"}
    if action not in mapping:
        return RedirectResponse(url=f"/projects/{project_id}",
                                status_code=303)
    conn = get_connection()
    try:
        _own_project_or_404(conn, project_id, me)
        try:
            engine.set_project_status(conn, project_id, me["id"],
                                      mapping[action])
            conn.commit()
        except ProjectError as exc:
            conn.rollback()
            return RedirectResponse(
                url=f"/projects/{project_id}?error={str(exc)[:120]}",
                status_code=303)
    finally:
        conn.close()
    return RedirectResponse(
        url=f"/projects/{project_id}?notice=project-{mapping[action]}",
        status_code=303)


@router.post("/projects/{project_id}/files")
def project_write_file(request: Request, project_id: int,
                       path: str = Form(""), content: str = Form(""),
                       csrf_token: str = Form("")):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not _limited(request):
        return RedirectResponse(url=f"/projects/{project_id}",
                                status_code=303)
    conn = get_connection()
    try:
        _own_project_or_404(conn, project_id, me)
        try:
            version = write_file(conn, project_id, path, content,
                                 updated_by=f"user:{me['id']}")
            engine.log_event(conn, project_id, f"user:{me['id']}",
                             "file.saved", f"{path} v{version}")
            conn.commit()
        except ProjectError as exc:
            conn.rollback()
            return RedirectResponse(
                url=f"/projects/{project_id}?error={str(exc)[:120]}",
                status_code=303)
    finally:
        conn.close()
    return RedirectResponse(
        url=f"/projects/{project_id}?notice=file-saved", status_code=303)


@router.post("/projects/{project_id}/checks")
def project_checks(request: Request, project_id: int,
                   command: str = Form(""),
                   confirm: str = Form(""),
                   csrf_token: str = Form("")):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not _limited(request):
        return RedirectResponse(url=f"/projects/{project_id}",
                                status_code=303)
    try:
        argv = shlex.split(command or "")
    except ValueError:
        argv = []
    conn = get_connection()
    try:
        _own_project_or_404(conn, project_id, me)
        try:
            result = engine.run_checks(
                conn, project_id, me["id"], argv=argv,
                approved=confirm.strip().lower() in ("yes", "on", "true",
                                                     "1"))
            conn.commit()
        except ProjectError as exc:
            conn.rollback()
            return RedirectResponse(
                url=f"/projects/{project_id}?error={str(exc)[:120]}",
                status_code=303)
    finally:
        conn.close()
    outcome = "passed" if result.ok else "failed"
    if result.timed_out:
        outcome = "timeout"
    return RedirectResponse(
        url=f"/projects/{project_id}?notice=checks-{outcome}",
        status_code=303)


@router.post("/projects/{project_id}/finalize")
def project_finalize(request: Request, project_id: int,
                     csrf_token: str = Form("")):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not _limited(request):
        return RedirectResponse(url=f"/projects/{project_id}",
                                status_code=303)
    conn = get_connection()
    try:
        _own_project_or_404(conn, project_id, me)
        try:
            engine.finalize(conn, project_id, me["id"])
            conn.commit()
        except ProjectError as exc:
            conn.rollback()
            return RedirectResponse(
                url=f"/projects/{project_id}?error={str(exc)[:120]}",
                status_code=303)
    finally:
        conn.close()
    return RedirectResponse(
        url=f"/projects/{project_id}?notice=project-done", status_code=303)
