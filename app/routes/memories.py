"""Memory review routes (owner-only)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import auth, memories
from ..config import settings
from ..db import get_connection, get_twin_by_user
from ..importers import import_upload
from ..import_pipeline import MAX_IMPORT_BYTES, accept_upload
from ..memories import MemoryError
from .accounts import current_user

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["memories"])


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _my_twin_id(request: Request) -> tuple[dict | None, int | None]:
    me = current_user(request)
    if me is None:
        return None, None
    conn = get_connection()
    try:
        twin = get_twin_by_user(conn, me["id"])
    finally:
        conn.close()
    return me, (twin["id"] if twin else None)


@router.get("/memories", response_class=HTMLResponse)
def memory_list(request: Request, notice: str = "", error: str = "",
                category: str = ""):
    me, twin_id = _my_twin_id(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    if twin_id is None:
        return RedirectResponse(url="/twin/new", status_code=303)
    picked = category if category in memories.CATEGORIES else ""
    conn = get_connection()
    try:
        groups = {
            status: [
                {"id": m["id"], "content": m["content"],
                 "source": m["source"], "category": m["category"],
                 "source_time": m["source_time"]}
                for m in memories.list_memories(conn, twin_id, status,
                                                picked or None)
            ]
            for status in ("pending", "approved", "rejected")
        }
    finally:
        conn.close()
    return templates.TemplateResponse(
        request, "memories.html",
        {"app_name": settings.app_name, "me": me, "groups": groups,
         "notice": notice, "error": error,
         "categories": memories.CATEGORIES,
         "picked_category": picked,
         "max_mb": MAX_IMPORT_BYTES // (1024 * 1024),
         "csrf_token": auth.csrf_for_request(request, me["id"])},
    )


@router.post("/memories/import")
async def memory_import(request: Request):
    me, twin_id = _my_twin_id(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    if twin_id is None:
        return RedirectResponse(url="/twin/new", status_code=303)
    form = await request.form()
    auth.require_session_csrf(request, form.get("csrf_token"), me["id"])
    if not auth.check_rate_limit(f"memimport:{me['id']}", 10, 3600):
        return RedirectResponse(url="/memories?error=rate-limited", status_code=303)
    if not auth.check_rate_limit(f"memwrite:{_ip(request)}", 60, 3600):
        return RedirectResponse(url="/memories?error=rate-limited", status_code=303)
    upload = form.get("export_file")
    filename = getattr(upload, "filename", "") or ""
    problem = accept_upload(filename, getattr(upload, "content_type", "") or "")
    if problem is not None:
        return RedirectResponse(url=f"/memories?error={problem}", status_code=303)
    raw = await upload.read(MAX_IMPORT_BYTES + 1)
    await upload.close()
    conn = get_connection()
    try:
        try:
            result = import_upload(filename, raw)
        except MemoryError as exc:
            return RedirectResponse(
                url=f"/memories?error={str(exc)[:120]}", status_code=303)
        added = duplicates = 0
        for candidate in result.candidates:
            mem_id = memories.create_memory(
                conn, twin_id=twin_id, content=candidate.content,
                source=candidate.source, category=candidate.category,
                source_time=candidate.source_time)
            if mem_id is None:
                duplicates += 1
            else:
                added += 1
        conn.commit()
    finally:
        conn.close()
    notice = (f"imported-{added}-duplicates-{duplicates}"
              f"-{result.provider}-skipped-{result.skipped}"
              f"-attachments-{result.attachments}"
              f"-review-{len(result.warnings)}")
    return RedirectResponse(url=f"/memories?notice={notice}",
                            status_code=303)


def _own_memory_or_404(conn, twin_id: int, mem_id: int):
    mem = memories.get_memory(conn, mem_id)
    if mem is None or mem["twin_id"] != twin_id:
        raise HTTPException(status_code=404, detail="Memory not found")
    return mem


@router.post("/memories/{mem_id}/approve")
def memory_approve(request: Request, mem_id: int, content: str = Form(""),
                   csrf_token: str = Form("")):
    me, twin_id = _my_twin_id(request)
    if me is None or twin_id is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not auth.check_rate_limit(f"memwrite:{_ip(request)}", 60, 3600):
        return RedirectResponse(url="/memories", status_code=303)
    conn = get_connection()
    try:
        mem = _own_memory_or_404(conn, twin_id, mem_id)
        edited = content.strip()
        try:
            if edited and edited != mem["content"]:
                memories.set_status(conn, mem_id, "approved", new_content=edited)
            else:
                memories.set_status(conn, mem_id, "approved")
            conn.commit()
        except MemoryError:
            pass
    finally:
        conn.close()
    return RedirectResponse(url="/memories", status_code=303)


@router.post("/memories/{mem_id}/reject")
def memory_reject(request: Request, mem_id: int, csrf_token: str = Form("")):
    me, twin_id = _my_twin_id(request)
    if me is None or twin_id is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not auth.check_rate_limit(f"memwrite:{_ip(request)}", 60, 3600):
        return RedirectResponse(url="/memories", status_code=303)
    conn = get_connection()
    try:
        _own_memory_or_404(conn, twin_id, mem_id)
        memories.set_status(conn, mem_id, "rejected")
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/memories", status_code=303)


@router.post("/memories/{mem_id}/delete")
def memory_delete(request: Request, mem_id: int, csrf_token: str = Form("")):
    me, twin_id = _my_twin_id(request)
    if me is None or twin_id is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not auth.check_rate_limit(f"memwrite:{_ip(request)}", 60, 3600):
        return RedirectResponse(url="/memories", status_code=303)
    conn = get_connection()
    try:
        _own_memory_or_404(conn, twin_id, mem_id)
        memories.delete_memory(conn, mem_id)
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url="/memories", status_code=303)
