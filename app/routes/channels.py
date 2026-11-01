"""Messaging routes (owner-only) + vendor webhooks (secret-signed)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import auth
from ..channels import (
    get_channel,
    log_inbound,
    parse_time_to_utc,
    process_due,
    resolve_recipient,
)
from ..channels import outbox
from ..channels.types import ChannelError
from ..config import settings
from ..db import get_connection
from .accounts import current_user

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["channels"])


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _limited(request: Request) -> bool:
    return auth.check_rate_limit(f"msgwrite:{_ip(request)}", 120, 3600)


def _own_account_or_404(conn, account_id: int, me: dict):
    account = outbox.get_account(conn, account_id, me["id"])
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


@router.get("/channels", response_class=HTMLResponse)
def channel_list(request: Request):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    conn = get_connection()
    try:
        accounts = [dict(a) for a in outbox.list_accounts(conn, me["id"])]
    finally:
        conn.close()
    hints = {}
    for name in ("telegram", "whatsapp", "messenger", "instagram", "mock"):
        hints[name] = get_channel(name).setup_requirements()
    return templates.TemplateResponse(
        request, "channels.html",
        {"app_name": settings.app_name, "me": me,
         "accounts": accounts, "hints": hints,
         "csrf_token": auth.csrf_for_request(request, me["id"])})


@router.post("/channels/connect")
def channel_connect(request: Request, channel: str = Form(""),
                    label: str = Form(""), token: str = Form(""),
                    csrf_token: str = Form("")):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not _limited(request):
        return RedirectResponse(url="/channels", status_code=303)
    credentials = {}
    if token.strip():
        key = "bot_token" if channel == "telegram" else "token"
        credentials = {key: token.strip()}
    conn = get_connection()
    try:
        try:
            account_id = outbox.create_account(
                conn, me["id"], channel, label,
                credentials=credentials)
            conn.commit()
        except ChannelError:
            conn.rollback()
            return RedirectResponse(url="/channels", status_code=303)
    finally:
        conn.close()
    return RedirectResponse(url=f"/channels/{account_id}",
                            status_code=303)


@router.get("/channels/{account_id}", response_class=HTMLResponse)
def channel_detail(request: Request, account_id: int, notice: str = "",
                   error: str = ""):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    conn = get_connection()
    try:
        account = _own_account_or_404(conn, account_id, me)
        contacts = outbox.list_contacts(conn, account_id)
        messages = [dict(r) for r in conn.execute(
            "SELECT m.*, c.display_name AS contact_name"
            " FROM outbox_messages m LEFT JOIN channel_contacts c"
            " ON c.id = m.contact_id WHERE m.account_id = ?"
            " ORDER BY m.id DESC LIMIT 50", (account_id,))]
        events = [dict(r) for r in conn.execute(
            "SELECT * FROM channel_events WHERE account_id = ?"
            " ORDER BY id DESC LIMIT 30", (account_id,))]
        permissions = outbox.account_permissions(conn, account_id)
    finally:
        conn.close()
    return templates.TemplateResponse(
        request, "channel.html",
        {"app_name": settings.app_name, "me": me,
         "account": dict(account), "contacts": contacts,
         "messages": messages, "events": events,
         "permissions": permissions, "notice": notice, "error": error,
         "csrf_token": auth.csrf_for_request(request, me["id"])})


@router.post("/channels/{account_id}/activate")
def channel_activate(request: Request, account_id: int,
                     csrf_token: str = Form("")):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not _limited(request):
        return RedirectResponse(url=f"/channels/{account_id}",
                                status_code=303)
    conn = get_connection()
    try:
        account = _own_account_or_404(conn, account_id, me)
        outbox.activate_account(conn, account["id"], me["id"])
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(
        url=f"/channels/{account_id}?notice=activated", status_code=303)


@router.post("/channels/{account_id}/revoke")
def channel_revoke(request: Request, account_id: int,
                   csrf_token: str = Form("")):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not _limited(request):
        return RedirectResponse(url=f"/channels/{account_id}",
                                status_code=303)
    conn = get_connection()
    try:
        account = _own_account_or_404(conn, account_id, me)
        outbox.revoke_account(conn, account["id"], me["id"])
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(
        url=f"/channels/{account_id}?notice=revoked", status_code=303)


@router.post("/channels/{account_id}/contacts")
def channel_add_contact(request: Request, account_id: int,
                        external_id: str = Form(""),
                        display_name: str = Form(""),
                        csrf_token: str = Form("")):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not _limited(request):
        return RedirectResponse(url=f"/channels/{account_id}",
                                status_code=303)
    conn = get_connection()
    try:
        account = _own_account_or_404(conn, account_id, me)
        try:
            outbox.add_contact(conn, account["id"], me["id"],
                               external_id=external_id,
                               display_name=display_name)
            conn.commit()
        except ChannelError as exc:
            conn.rollback()
            return RedirectResponse(
                url=f"/channels/{account_id}?error={str(exc)[:150]}",
                status_code=303)
    finally:
        conn.close()
    return RedirectResponse(
        url=f"/channels/{account_id}?notice=contact-added", status_code=303)


@router.post("/channels/{account_id}/draft")
def channel_draft(request: Request, account_id: int,
                  contact_id: str = Form(""),
                  body: str = Form(""), csrf_token: str = Form("")):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not _limited(request):
        return RedirectResponse(url=f"/channels/{account_id}",
                                status_code=303)
    try:
        contact = int(contact_id) if contact_id.strip() else None
    except ValueError:
        contact = None
    conn = get_connection()
    try:
        account = _own_account_or_404(conn, account_id, me)
        try:
            outbox.draft_message(conn, account["id"], me["id"],
                                 contact_id=contact, body=body)
            conn.commit()
        except ChannelError as exc:
            conn.rollback()
            return RedirectResponse(
                url=f"/channels/{account_id}?error={str(exc)[:150]}",
                status_code=303)
    finally:
        conn.close()
    return RedirectResponse(
        url=f"/channels/{account_id}?notice=drafted", status_code=303)


@router.post("/channels/{account_id}/messages/{message_id}/schedule")
def channel_schedule(request: Request, account_id: int, message_id: int,
                     when: str = Form(""), tz: str = Form("UTC"),
                     confirm: str = Form(""),
                     csrf_token: str = Form("")):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not _limited(request):
        return RedirectResponse(url=f"/channels/{account_id}",
                                status_code=303)
    conn = get_connection()
    try:
        account = _own_account_or_404(conn, account_id, me)
        message = outbox.get_message(message_id, me["id"])
        if message is None or message["account_id"] != account["id"]:
            raise HTTPException(status_code=404, detail="Not found")
        try:
            stamp = parse_time_to_utc(when, tz)
            outbox.schedule_message(
                conn, message_id, me["id"],
                scheduled_for_utc=stamp,
                confirmed=confirm.strip().lower() in ("yes", "on",
                                                      "true", "1"))
            conn.commit()
        except ChannelError as exc:
            conn.rollback()
            return RedirectResponse(
                url=f"/channels/{account_id}?error={str(exc)[:150]}",
                status_code=303)
    finally:
        conn.close()
    return RedirectResponse(
        url=f"/channels/{account_id}?notice=scheduled", status_code=303)


@router.post("/channels/{account_id}/messages/{message_id}/send")
def channel_send_now(request: Request, account_id: int, message_id: int,
                     confirm: str = Form(""),
                     csrf_token: str = Form("")):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not _limited(request):
        return RedirectResponse(url=f"/channels/{account_id}",
                                status_code=303)
    conn = get_connection()
    try:
        account = _own_account_or_404(conn, account_id, me)
        message = outbox.get_message(message_id, me["id"])
        if message is None or message["account_id"] != account["id"]:
            raise HTTPException(status_code=404, detail="Not found")
        try:
            outcome = outbox.send_now(
                conn, message_id, me["id"],
                confirmed=confirm.strip().lower() in ("yes", "on",
                                                      "true", "1"))
            conn.commit()
        except ChannelError as exc:
            conn.rollback()
            return RedirectResponse(
                url=f"/channels/{account_id}?error={str(exc)[:150]}",
                status_code=303)
    finally:
        conn.close()
    return RedirectResponse(
        url=f"/channels/{account_id}?notice=send-{outcome['status']}",
        status_code=303)


@router.post("/channels/{account_id}/messages/{message_id}/cancel")
def channel_cancel_message(request: Request, account_id: int,
                           message_id: int,
                           csrf_token: str = Form("")):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not _limited(request):
        return RedirectResponse(url=f"/channels/{account_id}",
                                status_code=303)
    conn = get_connection()
    try:
        account = _own_account_or_404(conn, account_id, me)
        message = outbox.get_message(message_id, me["id"])
        if message is None or message["account_id"] != account["id"]:
            raise HTTPException(status_code=404, detail="Not found")
        try:
            outbox.cancel_message(conn, message_id, me["id"])
            conn.commit()
        except ChannelError as exc:
            conn.rollback()
            return RedirectResponse(
                url=f"/channels/{account_id}?error={str(exc)[:150]}",
                status_code=303)
    finally:
        conn.close()
    return RedirectResponse(
        url=f"/channels/{account_id}?notice=cancelled", status_code=303)


@router.post("/channels/{account_id}/messages/{message_id}/retry")
def channel_retry_message(request: Request, account_id: int,
                          message_id: int,
                          csrf_token: str = Form("")):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not _limited(request):
        return RedirectResponse(url=f"/channels/{account_id}",
                                status_code=303)
    conn = get_connection()
    try:
        account = _own_account_or_404(conn, account_id, me)
        message = outbox.get_message(message_id, me["id"])
        if message is None or message["account_id"] != account["id"]:
            raise HTTPException(status_code=404, detail="Not found")
        try:
            outbox.retry_message(conn, message_id, me["id"])
            conn.commit()
        except ChannelError as exc:
            conn.rollback()
            return RedirectResponse(
                url=f"/channels/{account_id}?error={str(exc)[:150]}",
                status_code=303)
    finally:
        conn.close()
    return RedirectResponse(
        url=f"/channels/{account_id}?notice=requeued", status_code=303)


@router.post("/channels/{account_id}/process")
def channel_process_due(request: Request, account_id: int,
                        csrf_token: str = Form("")):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not _limited(request):
        return RedirectResponse(url=f"/channels/{account_id}",
                                status_code=303)
    conn = get_connection()
    try:
        account = _own_account_or_404(conn, account_id, me)
        report = process_due(conn, account_id=account["id"])
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(
        url=f"/channels/{account_id}"
            f"?notice=sent-{report['sent']}-failed-{report['failed']}",
        status_code=303)


@router.post("/channels/{account_id}/permissions")
def channel_permissions(request: Request, account_id: int,
                        auto_send: str = Form(""),
                        csrf_token: str = Form("")):
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not _limited(request):
        return RedirectResponse(url=f"/channels/{account_id}",
                                status_code=303)
    conn = get_connection()
    try:
        account = _own_account_or_404(conn, account_id, me)
        outbox.set_permissions(
            conn, account["id"], me["id"],
            {"auto_send": auto_send.strip().lower() in ("yes", "on",
                                                        "true", "1")})
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(
        url=f"/channels/{account_id}?notice=permissions-saved",
        status_code=303)


@router.post("/channels/{account_id}/quick-schedule")
def channel_quick_schedule(request: Request, account_id: int,
                           to: str = Form(""), body: str = Form(""),
                           when: str = Form(""), tz: str = Form("UTC"),
                           confirm: str = Form(""),
                           csrf_token: str = Form("")):
    """One form: recipient + text + time -> draft + schedule.

    Mirrors "send a message to Karma on Messenger at 11 PM": the
    recipient resolves (ambiguity refuses), the draft shows for
    review, and scheduling needs confirmation.
    """
    me = current_user(request)
    if me is None:
        return RedirectResponse(url="/login", status_code=303)
    auth.require_session_csrf(request, csrf_token, me["id"])
    if not _limited(request):
        return RedirectResponse(url=f"/channels/{account_id}",
                                status_code=303)
    conn = get_connection()
    try:
        account = _own_account_or_404(conn, account_id, me)
        try:
            contact = resolve_recipient(
                outbox.list_contacts(conn, account["id"]), to)
            msg_id = outbox.draft_message(
                conn, account["id"], me["id"],
                contact_id=contact["id"], body=body)
            stamp = parse_time_to_utc(when, tz)
            outbox.schedule_message(
                conn, msg_id, me["id"], scheduled_for_utc=stamp,
                confirmed=confirm.strip().lower() in ("yes", "on",
                                                      "true", "1"))
            conn.commit()
        except ChannelError as exc:
            conn.rollback()
            return RedirectResponse(
                url=f"/channels/{account_id}?error={str(exc)[:150]}",
                status_code=303)
    finally:
        conn.close()
    return RedirectResponse(
        url=f"/channels/{account_id}?notice=scheduled", status_code=303)


@router.get("/webhooks/{channel}/{account_id}")
def webhook_verify(channel: str, account_id: int, request: Request):
    """Meta handshake: echo hub.challenge when the token matches."""
    if channel not in ("whatsapp", "messenger", "instagram"):
        raise HTTPException(status_code=404, detail="No handshake here")
    params = request.query_params
    if params.get("hub.mode") != "subscribe":
        raise HTTPException(status_code=404, detail="Not found")
    conn = get_connection()
    try:
        row = conn.execute("SELECT webhook_secret FROM channel_accounts"
                           " WHERE id = ? AND channel = ?",
                           (account_id, channel)).fetchone()
    finally:
        conn.close()
    if row is None or not row["webhook_secret"]:
        raise HTTPException(status_code=404, detail="Not found")
    import hmac as _hmac

    if not _hmac.compare_digest(
            str(params.get("hub.verify_token", "")),
            row["webhook_secret"]):
        raise HTTPException(status_code=403, detail="Bad token")
    return JSONResponse({"hub.challenge": params.get("hub.challenge", "")})


@router.post("/webhooks/{channel}/{account_id}")
async def webhook_receive(channel: str, account_id: int,
                          request: Request):
    try:
        vendor = get_channel(channel)
    except ChannelError:
        raise HTTPException(status_code=404, detail="Not found")
    body = await request.body()
    if len(body) > 1024 * 1024:
        raise HTTPException(status_code=413, detail="Too large")
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT webhook_secret, status FROM channel_accounts"
            " WHERE id = ? AND channel = ?", (account_id, channel)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Not found")
        if not vendor.verify_webhook(row["webhook_secret"] or "", body,
                                     dict(request.headers)):
            raise HTTPException(status_code=403, detail="Bad signature")
        received = 0
        for incoming in vendor.parse_webhook(body):
            log_inbound(conn, account_id, incoming)
            received += 1
        conn.commit()
    finally:
        conn.close()
    return {"received": received}
