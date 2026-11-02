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

STATUSES = ("draft", "open", "matched", "chatting", "decided",
            "closed", "paused", "expired", "fulfilled")

#: Visibility rule for universal posts: a need is discoverable by other
#: twins only in these statuses. Drafts (not yet owner-confirmed),
#: paused, expired, fulfilled, and closed/withdrawn requests stay
#: private to the owner. There is no separate visibility column that
#: could contradict the lifecycle: confirming publishes, pausing /
#: expiring / fulfilling / closing withdraws.
SHARED_STATUSES = ("open", "matched", "chatting", "decided")

#: Quantity bounds for persistent searches.
MIN_QUANTITY = 1
MAX_QUANTITY = 100


def is_shared(req: Row) -> bool:
    """True when other twins may discover this request."""
    try:
        return req["status"] in SHARED_STATUSES
    except (KeyError, TypeError):
        return False


def request_title(need_text: str) -> str:
    """Display title derived from the need text (single source of truth).

    First sentence-ish fragment, capped at 80 chars. Stored titles
    would drift from edited need text; deriving keeps them in sync.
    """
    text = " ".join((need_text or "").split())
    if not text:
        return "(untitled request)"
    for end in (". ", "! ", "? ", ".\n", "!\n", "?\n"):
        cut = text.find(end)
        if 0 < cut <= 80:
            text = text[:cut]
            break
    text = text.rstrip(".!?").strip()
    if len(text) > 80:
        text = text[:77].rstrip() + "..."
    return text or "(untitled request)"

#: Owner-driven edges. "expired" and "fulfilled" are set by the system
#: (deadline sweep / quantity accounting in app/matcher.py); owners move
#: out of them via extend/reopen/cancel, never into them directly.
_ALLOWED: dict[str, tuple[str, ...]] = {
    "draft": ("open", "closed"),
    "open": ("matched", "paused", "closed"),
    "matched": ("chatting", "open", "paused", "closed"),
    "chatting": ("decided", "matched", "paused", "closed"),
    "decided": ("closed", "chatting", "paused"),
    "closed": (),
    "paused": ("open", "closed"),
    "expired": ("open", "closed"),
    "fulfilled": ("open", "closed"),
}


class RequestError(Exception):
    """User-facing request error (permission, state, or validation problem)."""


def validate_expires_at(value: str | None) -> str | None:
    """Normalize a deadline to the UTC stamp format, or None for none."""
    if value is None:
        return None
    clean = value.strip()
    if not clean:
        return None
    import datetime as _dt

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            moment = _dt.datetime.strptime(clean[:19], fmt)
        except ValueError:
            continue
        return moment.strftime("%Y-%m-%d %H:%M:%S")
    raise RequestError("Deadline must be a valid date and time.")


def validate_quantity(value) -> int:
    """Clamp a requested quantity into bounds (garbage becomes 1)."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return MIN_QUANTITY
    return max(MIN_QUANTITY, min(MAX_QUANTITY, number))


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
    expires_at: str | None = None,
    quantity_requested: int = MIN_QUANTITY,
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
            conditions=conditions, idempotency_key=key,
            expires_at=validate_expires_at(expires_at),
            quantity_requested=validate_quantity(quantity_requested))
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
                    conditions: dict | None = None,
                    expires_at: str | None = None,
                    quantity_requested: int | None = None) -> Row:
    """Owner reviews the parsed proposal and opens the request."""
    req = get_request(conn, req_id)
    if req is None:
        raise RequestError("Request not found.")
    if req["owner_user_id"] != user_id:
        raise RequestError("Only the owner's twin request can be confirmed.")
    if req["status"] != "draft":
        raise RequestError("Only a draft can be confirmed.")
    updates: dict = {"category": category, "requirements": requirements,
                     "conditions": conditions, "status": "open"}
    if expires_at is not None:
        updates["expires_at"] = validate_expires_at(expires_at)
    if quantity_requested is not None:
        updates["quantity_requested"] = validate_quantity(quantity_requested)
    update_request(conn, req_id, **{k: v for k, v in updates.items()
                                   if v is not None})
    conn.commit()
    return get_request(conn, req_id)


def update_need(conn: Connection, req_id: int, user_id: int | None,
                *, need_text: str | None = None,
                category: str | None = None,
                requirements: dict | None = None,
                expires_at: str | None | bool = None,
                quantity_requested: int | None = None) -> Row:
    """Owner edits a request after confirming. Status is preserved.

    Closed/withdrawn requests cannot be edited (reopen is a distinct,
    deliberate act the engine does not offer); drafts keep draft
    status so the review step still publishes them. Pass
    ``expires_at=False`` to clear the deadline.
    """
    req = get_request(conn, req_id)
    if req is None:
        raise RequestError("Request not found.")
    if req["owner_user_id"] != user_id:
        raise RequestError("Only the owner can edit this request.")
    if req["status"] == "closed":
        raise RequestError("Closed requests cannot be edited.")
    updates: dict = {}
    if need_text is not None:
        clean = (need_text or "").strip()[:2000]
        if not clean:
            raise RequestError("Please describe what you need.")
        updates["need_text"] = clean
    if category is not None:
        updates["category"] = (category or "other").strip()[:40] or "other"
    if requirements is not None:
        updates["requirements"] = requirements
    if expires_at is not None:
        updates["expires_at"] = (
            False if expires_at is False
            else validate_expires_at(expires_at))
    if quantity_requested is not None:
        updates["quantity_requested"] = validate_quantity(quantity_requested)
    if updates:
        update_request(conn, req_id, **updates)
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
