"""Conversation web routes (viewing, stepping, owner controls)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import auth, budget, conversations as engine
from ..avatars import avatar_svg
from ..config import settings
from ..db import get_connection, get_twin, get_twin_by_user
from ..memories import relevant_memories
from ..providers import get_provider
from .accounts import current_user

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["conversations"])


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _svg(seed: str | None) -> str:
    return avatar_svg(seed or "mira-fallback")


@router.get("/conversations", response_class=HTMLResponse)
def convo_list(request: Request, error: str = ""):
    me = current_user(request)
    conn = get_connection()
    try:
        mine = (
            [
                {"id": c["id"], "name_a": c["name_a"], "name_b": c["name_b"],
                 "status": c["status"], "purpose": c["purpose"],
                 "opp_title": c["opp_title"]}
                for c in engine.list_for_user(conn, me["id"])
            ]
            if me
            else []
        )
    finally:
        conn.close()
    return templates.TemplateResponse(
        request, "conversations.html",
        {"app_name": settings.app_name, "me": me, "mine": mine,
         "error": error},
    )


@router.post("/conversations/new")
def convo_create(
    request: Request,
    twin_a_id: int = Form(0),
    twin_b_id: int = Form(0),
    opportunity_id: str = Form(""),
    purpose: str = Form("open-chat"),
    is_demo: str | None = Form(None),
    csrf_token: str = Form(""),
):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not auth.check_rate_limit(f"convnew:{_ip(request)}", 30, 3600):
        return RedirectResponse(url="/conversations?error=rate-limited", status_code=303)
    opp_id = int(opportunity_id) if opportunity_id.strip().isdigit() else None
    conn = get_connection()
    try:
        try:
            conv_id = engine.create_conversation(
                conn, twin_a_id=twin_a_id, twin_b_id=twin_b_id,
                created_by_user_id=me["id"], opportunity_id=opp_id,
                purpose=purpose if purpose in engine.PURPOSES else "open-chat",
                is_demo=bool(is_demo),
            )
        except engine.ConversationError as exc:
            return RedirectResponse(
                url=f"/conversations?error={str(exc)[:120]}", status_code=303)
    finally:
        conn.close()
    return RedirectResponse(url=f"/conversations/{conv_id}", status_code=303)


def _load_conv_or_404(conn, conv_id: int):
    conv = engine.get_conversation(conn, conv_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


@router.get("/conversations/{conv_id}", response_class=HTMLResponse)
def convo_detail(request: Request, conv_id: int):
    me = current_user(request)
    uid = me["id"] if me else None
    conn = get_connection()
    try:
        conv = _load_conv_or_404(conn, conv_id)
        owner = engine.is_participant_owner(conn, conv, uid)
        if not owner and not conv["is_demo"]:
            raise HTTPException(status_code=404, detail="Conversation not found")
        twin_a, twin_b = get_twin(conn, conv["twin_a_id"]), get_twin(conn, conv["twin_b_id"])
        messages = [
            {"id": m["id"], "sender": m["sender"], "body": m["body"],
             "provider": m["provider"], "created_at": m["created_at"]}
            for m in engine.list_messages(conn, conv_id)
        ]
        audit_rows = (
            list(conn.execute(
                "SELECT action, detail, created_at FROM audit_events"
                " WHERE conv_id = ? ORDER BY id DESC LIMIT 20", (conv_id,)))
            if owner else []
        )
        can_see_request = False
        if conv["request_id"] and uid is not None:
            from ..db import get_request as _get_request

            linked = _get_request(conn, conv["request_id"])
            can_see_request = (
                linked is not None and linked["owner_user_id"] == uid)
        view = {
            "id": conv["id"], "status": conv["status"], "purpose": conv["purpose"],
            "turn": conv["turn"], "max_turns": conv["max_turns"],
            "is_demo": bool(conv["is_demo"]), "opp_title": conv["opp_title"],
            "opportunity_id": conv["opportunity_id"],
            "request_id": conv["request_id"], "req_category": conv["req_category"],
            "can_see_request": can_see_request,
            "name_a": conv["name_a"], "name_b": conv["name_b"],
            "seed_a": conv["seed_a"], "seed_b": conv["seed_b"],
            "owner_a_name": conv["owner_a_name"], "owner_b_name": conv["owner_b_name"],
            "decision_a": conv["decision_a"], "decision_b": conv["decision_b"],
            "turn_count": engine.twin_message_count(conn, conv_id),
        }
    finally:
        conn.close()
    return templates.TemplateResponse(
        request, "conversation.html",
        {"app_name": settings.app_name, "me": me, "conv": view, "messages": messages,
         "svg_a": _svg(view["seed_a"]), "svg_b": _svg(view["seed_b"]),
         "is_owner": owner, "audit": [dict(a) for a in audit_rows],
         "provider": get_provider().name,
         "csrf_token": auth.csrf_for_request(request, uid)},
    )


@router.get("/usage", response_class=HTMLResponse)
def usage_page(request: Request):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    conn = get_connection()
    try:
        summary = budget.usage_summary(conn, me["id"])
    finally:
        conn.close()
    return templates.TemplateResponse(
        request, "usage.html",
        {"app_name": settings.app_name, "me": me, "summary": summary,
         "provider": get_provider().name,
         "caps": {"conv": settings.budget_per_conversation_usd,
                  "user_daily": settings.budget_per_user_daily_usd}},
    )


@router.get("/conversations/{conv_id}/messages")
def convo_messages(request: Request, conv_id: int, since_id: int = 0):
    me = current_user(request)
    uid = me["id"] if me else None
    conn = get_connection()
    try:
        conv = engine.get_conversation(conn, conv_id)
        if conv is None or (
            not conv["is_demo"] and not engine.is_participant_owner(conn, conv, uid)
        ):
            raise HTTPException(status_code=404, detail="Conversation not found")
        messages = [
            {"id": m["id"], "sender": m["sender"], "body": m["body"],
             "provider": m["provider"]}
            for m in engine.list_messages(conn, conv_id, since_id=since_id)
        ]
        return JSONResponse({
            "messages": messages,
            "status": conv["status"],
            "turn_count": engine.twin_message_count(conn, conv_id),
            "max_turns": conv["max_turns"],
            "name_a": conv["name_a"], "name_b": conv["name_b"],
        })
    finally:
        conn.close()


def _memories_for_turn(conn, conv_id: int) -> list[str]:
    conv = engine.get_conversation(conn, conv_id)
    if conv is None:
        return []
    speaker_twin = conv["twin_a_id"] if conv["turn"] == "a" else conv["twin_b_id"]
    messages = engine.list_messages(conn, conv_id)
    query = messages[-1]["body"] if messages else ""
    return relevant_memories(conn, speaker_twin, query)


def _act(request: Request, conv_id: int, action: str, csrf_token: str = "", **kwargs):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if action in ("step", "auto") and not auth.check_rate_limit(
        f"step:{_ip(request)}", 120, 3600
    ):
        return RedirectResponse(url=f"/conversations/{conv_id}", status_code=303)
    if action not in ("step", "auto") and not auth.check_rate_limit(
        f"conactl:{_ip(request)}", 120, 3600
    ):
        return RedirectResponse(url=f"/conversations/{conv_id}", status_code=303)
    conn = get_connection()
    try:
        try:
            if action == "step":
                engine.step_conversation(
                    conn, conv_id, me["id"],
                    memories=_memories_for_turn(conn, conv_id))
            elif action == "auto":
                conv = engine.get_conversation(conn, conv_id)
                if conv is not None and engine.conversation_requires_approval(conn, conv):
                    engine.audit(conn, conv_id, "auto-blocked", me["id"],
                                 "require_approval")
                    conn.commit()
                    return RedirectResponse(url=f"/conversations/{conv_id}",
                                            status_code=303)
                for _ in range(max(1, min(12, int(kwargs.get("n", 6))))):
                    result = engine.step_conversation(
                        conn, conv_id, me["id"],
                        memories=_memories_for_turn(conn, conv_id))
                    if result["auto_closed"] or result["status"] != "active":
                        break
            elif action == "pause":
                engine.pause_conversation(conn, conv_id, me["id"])
            elif action == "resume":
                engine.resume_conversation(conn, conv_id, me["id"])
            elif action == "close":
                engine.close_conversation(conn, conv_id, me["id"])
            elif action == "note":
                engine.owner_note(conn, conv_id, me["id"], kwargs.get("text", ""))
            elif action == "decision":
                engine.set_decision(conn, conv_id, me["id"], kwargs.get("decision", ""))
            elif action == "recommend":
                conv = _load_conv_or_404(conn, conv_id)
                if not engine.is_participant_owner(conn, conv, me["id"]):
                    raise engine.ConversationError("Owners only.")
                if conv["purpose"] == "service" and conv["request_id"]:
                    text = engine.generate_service_summary(conn, conv_id)
                else:
                    # NOTE (hackathon compliance): the recruitment
                    # verdict summary was removed; twins never assess
                    # applicants. Service chats keep their summary.
                    raise engine.ConversationError("Nothing to summarize.")
                conn.execute(
                    "INSERT INTO messages (conv_id, sender, body, provider)"
                    " VALUES (?, 'system', ?, 'mock')", (conv_id, text))
                engine.audit(conn, conv_id, "recommend", me["id"], "")
                conn.commit()
        except engine.ConversationError:
            pass
    finally:
        conn.close()
    return RedirectResponse(url=f"/conversations/{conv_id}", status_code=303)


@router.post("/conversations/{conv_id}/step")
def convo_step(request: Request, conv_id: int, csrf_token: str = Form("")):
    return _act(request, conv_id, "step", csrf_token=csrf_token)


@router.post("/conversations/{conv_id}/auto")
def convo_auto(request: Request, conv_id: int, n: int = Form(6),
               csrf_token: str = Form("")):
    return _act(request, conv_id, "auto", n=n, csrf_token=csrf_token)


@router.post("/conversations/{conv_id}/pause")
def convo_pause(request: Request, conv_id: int, csrf_token: str = Form("")):
    return _act(request, conv_id, "pause", csrf_token=csrf_token)


@router.post("/conversations/{conv_id}/resume")
def convo_resume(request: Request, conv_id: int, csrf_token: str = Form("")):
    return _act(request, conv_id, "resume", csrf_token=csrf_token)


@router.post("/conversations/{conv_id}/close")
def convo_close(request: Request, conv_id: int, csrf_token: str = Form("")):
    return _act(request, conv_id, "close", csrf_token=csrf_token)


@router.post("/conversations/{conv_id}/note")
def convo_note(request: Request, conv_id: int, text: str = Form(""),
               csrf_token: str = Form("")):
    return _act(request, conv_id, "note", text=text, csrf_token=csrf_token)


@router.post("/conversations/{conv_id}/decision")
def convo_decision(request: Request, conv_id: int, decision: str = Form(""),
                   csrf_token: str = Form("")):
    return _act(request, conv_id, "decision", decision=decision,
                 csrf_token=csrf_token)


@router.post("/conversations/{conv_id}/recommend")
def convo_recommend(request: Request, conv_id: int, csrf_token: str = Form("")):
    return _act(request, conv_id, "recommend", csrf_token=csrf_token)
