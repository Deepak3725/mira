"""Seeker-request lifecycle engine (bot-to-bot Phase 1).

A request is a twin's structured need (taxi ride, hire, service, supply,
collaboration -- the engine treats categories as data, never as code
branches). Lifecycle: draft -> open -> matched -> chatting -> decided ->
closed. Owners review the parsed proposal before a draft goes open.

Idempotency: every creation carries an idempotency key, unique per owner.
Retried submissions return the existing request instead of duplicating it.
"""

from __future__ import annotations

import json
import secrets
from sqlite3 import Connection, Row
from typing import Any

from .db import (
    DUPLICATE_ERRORS,
    create_request,
    get_request,
    get_request_by_key,
    get_twin,
    update_request,
)

STATUSES = ("draft", "open", "matched", "chatting", "decided", "closed")

_ALLOWED: dict[str, tuple[str, ...]] = {
    "draft": ("open", "closed"),
    "open": ("matched", "closed"),
    "matched": ("chatting", "open", "closed"),
    "chatting": ("decided", "matched", "closed"),
    "decided": ("closed", "chatting"),
    "closed": (),
}


class RequestError(Exception):
    """User-facing request error (permission, state, or validation problem)."""


def _parse(raw: Any, fallback):
    try:
        parsed = json.loads(raw or "")
    except (ValueError, TypeError):
        return fallback
    return parsed if isinstance(parsed, type(fallback)) else fallback


def request_requirements(req: Row) -> dict:
    return _parse(req["requirements_json"], {})


def request_conditions(req: Row) -> dict:
    return _parse(req["conditions_json"], {})


def twin_conditions(twin: Row) -> dict:
    return _parse(twin["conditions_json"] if "conditions_json" in twin.keys() else "{}", {})


def effective_conditions(twin: Row, req: Row | None) -> dict:
    """Twin-level conditions overridden by per-request conditions."""
    merged = dict(twin_conditions(twin))
    if req is not None:
        merged.update(request_conditions(req))
    return merged


def create_need(
    conn: Connection,
    *,
    twin_id: int,
    owner_user_id: int,
    category: str,
    need_text: str,
    requirements: dict | None = None,
    conditions: dict | None = None,
    idempotency_key: str | None = None,
) -> tuple[int, bool]:
    """Create a request. Returns (id, created). Retries return (id, False)."""
    twin = get_twin(conn, twin_id)
    if twin is None:
        raise RequestError("Twin does not exist.")
    if twin["user_id"] != owner_user_id:
        raise RequestError("You can only post requests for your own twin.")
    need_text = (need_text or "").strip()[:2000]
    if not need_text:
        raise RequestError("Please describe what you need.")
    key = (idempotency_key or "").strip()[:64] or secrets.token_hex(16)
    try:
        req_id = create_request(
            conn, twin_id=twin_id, owner_user_id=owner_user_id,
            category=(category or "other").strip()[:40] or "other",
            need_text=need_text, requirements=requirements,
            conditions=conditions, idempotency_key=key)
        conn.commit()
        return req_id, True
    except DUPLICATE_ERRORS:
        conn.rollback()
        existing = get_request_by_key(conn, owner_user_id, key)
        if existing is None:
            raise
        return existing["id"], False


def confirm_request(conn: Connection, req_id: int, user_id: int | None,
                    *, category: str | None = None,
                    requirements: dict | None = None,
                    conditions: dict | None = None) -> Row:
    """Owner reviews the parsed proposal and opens the request."""
    req = get_request(conn, req_id)
    if req is None:
        raise RequestError("Request not found.")
    if req["owner_user_id"] != user_id:
        raise RequestError("Only the owner's twin request can be confirmed.")
    if req["status"] != "draft":
        raise RequestError("Only a draft can be confirmed.")
    update_request(conn, req_id, category=category, requirements=requirements,
                   conditions=conditions, status="open")
    conn.commit()
    return get_request(conn, req_id)


def transition(conn: Connection, req_id: int, user_id: int | None, to_status: str) -> Row:
    req = get_request(conn, req_id)
    if req is None:
        raise RequestError("Request not found.")
    if req["owner_user_id"] != user_id:
        raise RequestError("Only the owner can change this request.")
    if to_status not in _ALLOWED.get(req["status"], ()):
        raise RequestError(f"Cannot move a {req['status']} request to {to_status}.")
    update_request(conn, req_id, status=to_status)
    conn.commit()
    return get_request(conn, req_id)


def mark_matched(conn: Connection, req_id: int) -> None:
    req = get_request(conn, req_id)
    if req is not None and req["status"] == "open":
        update_request(conn, req_id, status="matched")
        conn.commit()


def mark_chatting(conn: Connection, req_id: int) -> None:
    req = get_request(conn, req_id)
    if req is not None and req["status"] in ("open", "matched"):
        update_request(conn, req_id, status="chatting")
        conn.commit()
