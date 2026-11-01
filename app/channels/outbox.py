"""Outbox engine: accounts, contacts, drafts, scheduling, delivery.

Safety contract: drafts show the message + recipient before anything
sends; scheduling needs per-message confirmation unless the account
has an explicit auto_send policy; ambiguous recipients never send;
duplicate idempotency keys never double-send; every attempt leaves
an audit event; a message is 'sent' only with a vendor receipt.
Delivery runs on explicit triggers (owner button now, worker later):
the queue itself is persistent, so nothing is lost between runs.
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
import sqlite3
import uuid

from ..db import driver_name, insert_returning_id
from .base import http_post_json
from .registry import get_channel
from .types import (
    ChannelError,
    ChannelNotConfigured,
    DeliveryReceipt,
    IncomingMessage,
)

ACCOUNT_STATUSES = ("pending", "active", "revoked")
MESSAGE_STATUSES = ("draft", "queued", "sending", "sent", "delivered",
                    "failed", "cancelled")
CHANNEL_NAMES = ("telegram", "whatsapp", "messenger", "instagram", "mock")


def _now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _touch(conn, row_id: int) -> None:
    stamp = "now()" if driver_name(conn) == "postgres" else "datetime('now')"
    conn.execute("UPDATE channel_accounts SET updated_at = "
                 f"{stamp} WHERE id = ?", (row_id,))


def _touch_msg(conn, row_id: int) -> None:
    stamp = "now()" if driver_name(conn) == "postgres" else "datetime('now')"
    conn.execute("UPDATE outbox_messages SET updated_at = "
                 f"{stamp} WHERE id = ?", (row_id,))


def log_channel_event(conn, account_id: int, kind: str,
                      detail: str = "", direction: str = "out") -> None:
    conn.execute(
        "INSERT INTO channel_events (account_id, direction, kind, detail)"
        " VALUES (?, ?, ?, ?)",
        (account_id, direction, kind, (detail or "")[:2000]))


def create_account(conn, owner_user_id: int, channel: str, label: str,
                   credentials: dict | None = None,
                   permissions: dict | None = None) -> int:
    name = (channel or "").lower()
    if name not in CHANNEL_NAMES:
        raise ChannelError(f"Unknown channel {channel!r}.")
    account_id = insert_returning_id(
        conn,
        "INSERT INTO channel_accounts (owner_user_id, channel, label,"
        " credentials_json, webhook_secret, permissions_json)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (owner_user_id, name, (label or "")[:120],
         json.dumps(credentials or {}),
         secrets.token_hex(16),
         json.dumps(permissions or {})))
    log_channel_event(conn, account_id, "account.connected",
                      f"{name} {(label or '')[:80]}")
    return account_id


def get_account(conn, account_id: int, owner_user_id: int):
    row = conn.execute("SELECT * FROM channel_accounts WHERE id = ?",
                       (account_id,)).fetchone()
    if row is None or row["owner_user_id"] != owner_user_id:
        return None
    return row


def list_accounts(conn, owner_user_id: int) -> list:
    return list(conn.execute(
        "SELECT id, channel, label, status, created_at"
        " FROM channel_accounts WHERE owner_user_id = ? ORDER BY id",
        (owner_user_id,)))


def activate_account(conn, account_id: int, owner_user_id: int) -> None:
    account = get_account(conn, account_id, owner_user_id)
    if account is None:
        raise ChannelError("Account not found.")
    conn.execute("UPDATE channel_accounts SET status = 'active'"
                 " WHERE id = ?", (account_id,))
    _touch(conn, account_id)
    log_channel_event(conn, account_id, "account.activated")


def revoke_account(conn, account_id: int, owner_user_id: int) -> None:
    account = get_account(conn, account_id, owner_user_id)
    if account is None:
        raise ChannelError("Account not found.")
    conn.execute("UPDATE channel_accounts SET status = 'revoked',"
                 " credentials_json = '{}', webhook_secret = ''"
                 " WHERE id = ?", (account_id,))
    _touch(conn, account_id)
    log_channel_event(conn, account_id, "account.revoked")


def account_permissions(conn, account_id: int) -> dict:
    row = conn.execute("SELECT permissions_json FROM channel_accounts"
                       " WHERE id = ?", (account_id,)).fetchone()
    try:
        data = json.loads(row["permissions_json"] or "{}")
    except (ValueError, TypeError):
        data = {}
    return data if isinstance(data, dict) else {}


def set_permissions(conn, account_id: int, owner_user_id: int,
                    permissions: dict) -> None:
    if get_account(conn, account_id, owner_user_id) is None:
        raise ChannelError("Account not found.")
    clean = {str(k)[:40]: bool(v) for k, v in (permissions or {}).items()}
    conn.execute("UPDATE channel_accounts SET permissions_json = ?"
                 " WHERE id = ?", (json.dumps(clean), account_id))
    _touch(conn, account_id)
    log_channel_event(conn, account_id, "account.permissions",
                      ",".join(sorted(clean)))


def add_contact(conn, account_id: int, owner_user_id: int, *,
                external_id: str, display_name: str) -> int:
    if get_account(conn, account_id, owner_user_id) is None:
        raise ChannelError("Account not found.")
    if not (external_id or "").strip() or not (display_name or "").strip():
        raise ChannelError("Contacts need an id and a display name.")
    try:
        contact_id = insert_returning_id(
            conn,
            "INSERT INTO channel_contacts (account_id, external_id,"
            " display_name) VALUES (?, ?, ?)",
            (account_id, external_id.strip()[:200],
             display_name.strip()[:200]))
    except Exception as exc:
        if "UNIQUE" in str(exc).upper() or "unique" in str(exc).lower():
            raise ChannelError("That contact id is already saved.") from exc
        raise
    log_channel_event(conn, account_id, "contact.added",
                      display_name.strip()[:120])
    return contact_id


def list_contacts(conn, account_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM channel_contacts WHERE account_id = ? ORDER BY id",
        (account_id,))]


def draft_message(conn, account_id: int, owner_user_id: int, *,
                  contact_id: int | None, body: str,
                  idempotency_key: str | None = None) -> int:
    if get_account(conn, account_id, owner_user_id) is None:
        raise ChannelError("Account not found.")
    text = (body or "").strip()
    if not text:
        raise ChannelError("Messages cannot be empty.")
    if contact_id is not None:
        contact = conn.execute(
            "SELECT id FROM channel_contacts WHERE id = ?"
            " AND account_id = ?", (contact_id, account_id)).fetchone()
        if contact is None:
            raise ChannelError("Contact not found.")
    key = (idempotency_key or "").strip() or uuid.uuid4().hex
    try:
        msg_id = insert_returning_id(
            conn,
            "INSERT INTO outbox_messages (account_id, contact_id, body,"
            " idempotency_key) VALUES (?, ?, ?, ?)",
            (account_id, contact_id, text[:4000], key[:64]))
    except Exception as exc:
        if "UNIQUE" in str(exc).upper() or "unique" in str(exc).lower():
            raise ChannelError(
                "Duplicate message: this idempotency key was used.") from exc
        raise
    log_channel_event(conn, account_id, "message.drafted", f"#{msg_id}")
    return msg_id


def get_message(conn, message_id: int, owner_user_id: int):
    row = conn.execute(
        "SELECT m.*, a.owner_user_id AS owner FROM outbox_messages m"
        " JOIN channel_accounts a ON a.id = m.account_id"
        " WHERE m.id = ?", (message_id,)).fetchone()
    if row is None or row["owner"] != owner_user_id:
        return None
    return row


def schedule_message(conn, message_id: int, owner_user_id: int, *,
                     scheduled_for_utc: str, confirmed: bool) -> None:
    message = get_message(conn, message_id, owner_user_id)
    if message is None:
        raise ChannelError("Message not found.")
    if message["status"] not in ("draft", "failed"):
        raise ChannelError(
            f"Only drafts or failed messages schedule (is {message['status']}).")
    if message["contact_id"] is None:
        raise ChannelError("Set the recipient before scheduling.")
    permissions = account_permissions(conn, message["account_id"])
    if not confirmed and not permissions.get("auto_send"):
        raise ChannelError("Confirm the recipient and text first.")
    conn.execute("UPDATE outbox_messages SET status = 'queued',"
                 " scheduled_for = ?, last_error = '' WHERE id = ?",
                 (scheduled_for_utc, message_id))
    _touch_msg(conn, message_id)
    log_channel_event(conn, message["account_id"], "message.scheduled",
                      f"#{message_id} for {scheduled_for_utc}")


def cancel_message(conn, message_id: int, owner_user_id: int) -> None:
    message = get_message(conn, message_id, owner_user_id)
    if message is None:
        raise ChannelError("Message not found.")
    if message["status"] not in ("draft", "queued"):
        raise ChannelError(
            f"Only drafts or queued messages cancel (is {message['status']}).")
    conn.execute("UPDATE outbox_messages SET status = 'cancelled'"
                 " WHERE id = ?", (message_id,))
    _touch_msg(conn, message_id)
    log_channel_event(conn, message["account_id"], "message.cancelled",
                      f"#{message_id}")


def retry_message(conn, message_id: int, owner_user_id: int) -> None:
    message = get_message(conn, message_id, owner_user_id)
    if message is None:
        raise ChannelError("Message not found.")
    if message["status"] != "failed":
        raise ChannelError("Only failed messages retry.")
    conn.execute("UPDATE outbox_messages SET status = 'queued',"
                 " last_error = '' WHERE id = ?", (message_id,))
    _touch_msg(conn, message_id)
    log_channel_event(conn, message["account_id"], "message.retried",
                      f"#{message_id}")


def _attempt(conn, message, post) -> dict:
    account = conn.execute("SELECT * FROM channel_accounts WHERE id = ?",
                           (message["account_id"],)).fetchone()
    channel = get_channel(account["channel"])
    try:
        credentials = json.loads(account["credentials_json"] or "{}")
    except ValueError:
        credentials = {}
    contact = conn.execute("SELECT * FROM channel_contacts WHERE id = ?",
                           (message["contact_id"],)).fetchone()
    conn.execute("UPDATE outbox_messages SET status = 'sending',"
                 " attempts = attempts + 1 WHERE id = ?",
                 (message["id"],))
    _touch_msg(conn, message["id"])
    try:
        receipt: DeliveryReceipt = channel.send(
            post or http_post_json, credentials if isinstance(
                credentials, dict) else {},
            contact["external_id"], message["body"],
            message["idempotency_key"])
    except (ChannelError, ChannelNotConfigured) as exc:
        conn.execute("UPDATE outbox_messages SET status = 'failed',"
                     " last_error = ? WHERE id = ?",
                     (str(exc)[:500], message["id"]))
        _touch_msg(conn, message["id"])
        log_channel_event(conn, account["id"], "message.failed",
                          f"#{message['id']}: {exc}"[:500])
        return {"id": message["id"], "status": "failed",
                "error": str(exc)[:200]}
    conn.execute("UPDATE outbox_messages SET status = 'sent',"
                 " provider_msg_id = ?, last_error = '' WHERE id = ?",
                 (receipt.provider_msg_id[:200], message["id"]))
    _touch_msg(conn, message["id"])
    log_channel_event(conn, account["id"], "message.sent",
                      f"#{message['id']} receipt {receipt.provider_msg_id}")
    return {"id": message["id"], "status": "sent",
            "receipt": receipt.provider_msg_id}


def send_now(conn, message_id: int, owner_user_id: int, *,
             confirmed: bool, post=None) -> dict:
    """Send one draft immediately (confirmation rules apply)."""
    message = get_message(conn, message_id, owner_user_id)
    if message is None:
        raise ChannelError("Message not found.")
    if message["status"] != "draft":
        raise ChannelError(
            f"Only drafts send immediately (is {message['status']}).")
    if message["contact_id"] is None:
        raise ChannelError("Set the recipient before sending.")
    account = get_account(conn, message["account_id"], owner_user_id)
    if account is None or account["status"] != "active":
        raise ChannelError("Activate the account before sending.")
    permissions = account_permissions(conn, account["id"])
    if not confirmed and not permissions.get("auto_send"):
        raise ChannelError("Confirm the recipient and text first.")
    return _attempt(conn, dict(message), post)


def process_due(conn, now_utc: str | None = None,
                post=None, account_id: int | None = None) -> dict:
    """Send every due queued message on active accounts. Returns counts."""
    now = now_utc or _now_utc()
    query = (
        "SELECT m.* FROM outbox_messages m"
        " JOIN channel_accounts a ON a.id = m.account_id"
        " WHERE m.status = 'queued' AND m.scheduled_for <= ?"
        " AND a.status = 'active'")
    params: list = [now]
    if account_id is not None:
        query += " AND m.account_id = ?"
        params.append(account_id)
    query += " ORDER BY m.scheduled_for, m.id"
    rows = conn.execute(query, params).fetchall()
    outcomes: list[dict] = []
    for row in rows:
        message = dict(row)
        if message["contact_id"] is None:
            conn.execute("UPDATE outbox_messages SET status = 'failed',"
                         " last_error = 'Recipient missing.' WHERE id = ?",
                         (message["id"],))
            outcomes.append({"id": message["id"], "status": "failed",
                             "error": "Recipient missing."})
            continue
        outcomes.append(_attempt(conn, message, post))
    sent = sum(1 for o in outcomes if o["status"] == "sent")
    return {"attempted": len(outcomes), "sent": sent,
            "failed": len(outcomes) - sent, "outcomes": outcomes}


def log_inbound(conn, account_id: int,
                incoming: IncomingMessage) -> int | None:
    """Audit one inbound message; save unknown senders as contacts."""
    existing = conn.execute(
        "SELECT id FROM channel_contacts WHERE account_id = ?"
        " AND external_id = ?", (account_id,
                                 incoming.external_id)).fetchone()
    if existing is None and incoming.external_id:
        contact_id = insert_returning_id(
            conn,
            "INSERT INTO channel_contacts (account_id, external_id,"
            " display_name) VALUES (?, ?, ?)",
            (account_id, incoming.external_id[:200],
             (incoming.sender_name or incoming.external_id)[:200]))
    else:
        contact_id = existing["id"] if existing else None
    log_channel_event(conn, account_id, "message.received",
                      f"from {incoming.external_id}: "
                      f"{incoming.body[:200]}", direction="in")
    return contact_id
