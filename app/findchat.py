"""Find Something conversation service (owner <-> own twin).

One user message in -> persisted user message, real need/search
handling through the existing engines, persisted twin replies.
Deterministic and $0 on the mock provider; the twin never claims a
real LLM is running and never invents matches.

Pipeline:
  smalltalk/status -> conversational reply only (no need created)
  answer to a pending question -> merge slots into that need, re-match
  anything else -> interpret -> create + confirm need -> match inline
"""

from __future__ import annotations

import datetime as _dt
import json as _json
import re
import uuid
from sqlite3 import Connection, Row

from . import matcher
from . import requests as need_engine
from .db import (
    create_twin_chat,
    get_opportunity,
    get_twin,
    list_matches_for_search,
    list_requests_for_user,
    list_twin_chats,
    resolve_twin_chat,
)
from .intent import interpret_need
from .request_parse import parse_need

#: Default search window when the owner states no duration (1 week).
DEFAULT_SEARCH_MINUTES = 10080

MAX_MESSAGE = 2000

_GREETING = re.compile(
    r"^\s*(hi|hello|hey|yo|good\s+(morning|afternoon|evening))"
    r"[\s.!]*$", re.IGNORECASE)
_CAPABILITY = re.compile(
    r"what can you do|who are you|help\b|^help$", re.IGNORECASE)
_THANKS = re.compile(r"^\s*(thanks|thank you|thx)[\s.!]*$", re.IGNORECASE)
_BYE = re.compile(r"^\s*(bye|goodbye|see you)[\s.!]*$", re.IGNORECASE)
_STATUS = re.compile(
    r"\b(my\s+(requests|searches|results|matches)|what.*looking for"
    r"|status|show\s+my)\b", re.IGNORECASE)
_NEED_VERB = re.compile(
    r"\bi\s+(need|want|am\s+looking\s+for)\b|\blooking\s+for\b"
    r"|\bfind(\s+me)?\b|\bsearch(ing)?\s+for\b|\bhiring\b|\bget\s+me\b"
    r"|\bneed\b|\bwant\b", re.IGNORECASE)


class FindChatError(ValueError):
    """User-facing chat failure (message preserved by the caller)."""


def _profile(conn: Connection, twin_id: int) -> dict:
    twin = get_twin(conn, twin_id)
    if twin is None:
        return {}
    try:
        profile = _json.loads(twin["profile_json"] or "{}")
    except (ValueError, TypeError):
        return {}
    return profile if isinstance(profile, dict) else {}


def _profile_line(profile: dict) -> str:
    """One honest line about which profile context the twin used."""
    skills = [s for s in profile.get("skills", [])
              if isinstance(s, str) and s.strip()][:4]
    location = (profile.get("location") or "").strip()
    bits = []
    if skills:
        bits.append("skills: " + ", ".join(skills))
    if location:
        bits.append("based in " + location[:60])
    if not bits:
        return ""
    return "Using your profile (" + "; ".join(bits) + ")."


def _material_question(category: str, requirements: dict) -> str:
    """One follow-up only when a gap blocks discovery, else ''."""
    if category == "transport" and not (
            requirements.get("origin") or requirements.get("destination")):
        return ("Where should the trip start and end? "
                "A maximum budget helps too, if you have one.")
    return ""


def _deadline_label(expires_at: str | None) -> str:
    if not expires_at:
        return "until you cancel it"
    return "until " + expires_at[:10]


def _is_answer(text: str, twin_messages: list) -> Row | None:
    """Return the newest unanswered question this message answers."""
    pending = next(
        (m for m in reversed(twin_messages)
         if m["kind"] == "question" and m["ref_type"] == "request"),
        None)
    if pending is None:
        return None
    words = (text or "").split()
    if len(words) > 20 or _NEED_VERB.search(text or ""):
        return None
    return pending


def _smalltalk_reply(text: str, twin_name: str) -> str | None:
    clean = (text or "").strip()
    if _GREETING.match(clean):
        return (f"Hi, I'm {twin_name}. Tell me what you're looking for"
                " — a job, a ride, a tutor, anything — and I'll search"
                " for it.")
    if _CAPABILITY.search(clean):
        return ("I search for things you need and coordinate with other"
                " twins. Just describe what you're looking for in words"
                " — no forms, no categories to pick.")
    if _THANKS.match(clean):
        return "Anytime. Tell me when you need something found."
    if _BYE.match(clean):
        return "See you soon. Your searches keep running until you stop them."
    return None


def _status_reply(conn: Connection, user_id: int) -> str:
    rows = [r for r in list_requests_for_user(conn, user_id)
            if r["status"] not in ("draft", "closed")]
    if not rows:
        return ("You have no active searches. Describe what you're"
                " looking for and I'll start one.")
    lines = [f"Your searches ({len(rows)}):"]
    for row in rows[:10]:
        numbers = matcher.fulfillment(conn, row["id"])
        lines.append(
            f"- {need_engine.request_title(row['need_text'])}:"
            f" {row['status']}, accepted {numbers['accepted']}"
            f" of {numbers['requested']}.")
    return "\n".join(lines)


def _answer_question(conn: Connection, user_id: int, twin_id: int,
                     text: str, pending: Row) -> dict:
    """Merge a short answer into the need that asked the question."""
    req = need_engine.get_request(conn, pending["ref_id"])
    if req is None or req["owner_user_id"] != user_id:
        raise FindChatError(
            "That search is gone; please describe your need again.")
    if req["status"] in ("closed", "expired", "fulfilled"):
        raise FindChatError(
            "That search already ended; please start a new one.")
    parsed = parse_need(text)
    merged = dict(need_engine.request_requirements(req))
    added = []
    for key in ("origin", "destination", "when_text", "when_iso",
                "budget_max", "budget_currency", "skills"):
        if parsed["requirements"].get(key) and key not in merged:
            merged[key] = parsed["requirements"][key]
            added.append(key)
    if not added:
        reply = ("I couldn't use that as an answer — could you say it"
                 " differently? Or describe a new need and I'll search"
                 " for it.")
        _say(conn, user_id, twin_id, reply, kind="question",
             ref_type="request", ref_id=req["id"])
        return {"need_id": req["id"], "matches": [], "replies": [reply]}
    need_engine.update_need(conn, req["id"], user_id, requirements=merged)
    resolve_twin_chat(conn, pending["id"], user_id)
    matcher.trigger_matching(conn, "request_saved", "request", req["id"])
    matches = match_cards(conn, req["id"])
    if matches:
        reply = (f"Thanks — updated your search. I found"
                 f" {len(matches)} relevant result"
                 f"{'s' if len(matches) != 1 else ''}:")
    else:
        reply = ("Thanks — updated your search. Nothing suitable yet;"
                 f" I'll keep looking"
                 f" {_deadline_label(req['expires_at'])}.")
    _say(conn, user_id, twin_id, reply, kind="results",
         ref_type="request", ref_id=req["id"])
    return {"need_id": req["id"], "matches": matches,
            "replies": [reply]}


def match_cards(conn: Connection, req_id: int) -> list[dict]:
    out = []
    for match in list_matches_for_search(conn, req_id):
        peer_title = ""
        peer_kind = match["counterpart_type"]
        if peer_kind == "opportunity":
            opp = get_opportunity(conn, match["counterpart_id"])
            if opp is not None:
                peer_title = opp["title"]
        else:
            peer = need_engine.get_request(conn, match["counterpart_id"])
            if peer is not None:
                peer_title = need_engine.request_title(peer["need_text"])
        if not peer_title:
            continue
        out.append({
            "id": match["id"],
            "title": peer_title,
            "kind": "offer" if peer_kind == "opportunity" else "need",
            "reason": match["reason"] or "",
            "status": match["status"],
        })
    return out


def _say(conn: Connection, user_id: int, twin_id: int, body: str,
         kind: str = "text", ref_type: str = "",
         ref_id: int = 0) -> None:
    create_twin_chat(conn, user_id=user_id, twin_id=twin_id, role="twin",
                     body=body, kind=kind, ref_type=ref_type,
                     ref_id=ref_id)


def _created_within(stamp, window_s: int) -> bool:
    """True when a chat created_at stamp is at most window_s old."""
    try:
        if isinstance(stamp, str):
            moment = _dt.datetime.strptime(
                stamp[:19], "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=_dt.timezone.utc)
        else:
            moment = stamp if stamp.tzinfo is not None else stamp.replace(
                tzinfo=_dt.timezone.utc)
        age = (_dt.datetime.now(_dt.timezone.utc) - moment).total_seconds()
        return 0 <= age <= window_s
    except (ValueError, TypeError, AttributeError):
        return False


def is_duplicate_submit(conn: Connection, user_id: int, text: str,
                        window_s: int = 60) -> bool:
    """True when the newest user message is the same recent text.

    Guards the Send button against double-click/double-Enter: the
    twin must not create two identical searches. Unparseable stamps
    fail open to normal handling (same as no dedupe).
    """
    clean = " ".join((text or "").split())
    if not clean:
        return False
    row = conn.execute(
        "SELECT body, created_at FROM twin_chats"
        " WHERE user_id = ? AND role = 'user'"
        " ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
    if row is None:
        return False
    prior = " ".join((row["body"] or "").split())
    return prior == clean and _created_within(row["created_at"], window_s)


def post_message(conn: Connection, *, user_id: int, twin_id: int,
                 text: str, twin_name: str = "twin") -> dict:
    """Handle one Find message. Returns need/matches/replies for display."""
    clean = " ".join((text or "").split())
    if not clean:
        raise FindChatError("Please type what you're looking for.")
    if len(clean) > MAX_MESSAGE:
        raise FindChatError(
            f"Please keep messages under {MAX_MESSAGE} characters.")
    create_twin_chat(conn, user_id=user_id, twin_id=twin_id,
                     role="user", body=clean)
    conn.commit()

    has_need_verb = bool(_NEED_VERB.search(clean))
    small = None if has_need_verb else _smalltalk_reply(clean, twin_name)
    if small is not None:
        _say(conn, user_id, twin_id, small)
        conn.commit()
        return {"need_id": None, "matches": [], "replies": [small]}
    if _STATUS.search(clean) and not has_need_verb:
        reply = _status_reply(conn, user_id)
        _say(conn, user_id, twin_id, reply)
        conn.commit()
        return {"need_id": None, "matches": [], "replies": [reply]}

    # The just-stored user message is newest; a pending question is
    # an unanswered twin question before it.
    prior = [m for m in list_twin_chats(conn, user_id, limit=10)
             if m["role"] == "twin"]
    question = _is_answer(clean, prior)
    if question is not None:
        out = _answer_question(conn, user_id, twin_id, clean, question)
        conn.commit()
        return out

    parsed = parse_need(clean)
    interp = interpret_need(clean, conn=conn, user_id=user_id)
    minutes = parsed["deadline_minutes"] or DEFAULT_SEARCH_MINUTES
    expires = matcher.plus_minutes(matcher.now_stamp(), minutes)
    quantity = parsed["quantity"] or 1
    req_id, _ = need_engine.create_need(
        conn, twin_id=twin_id, owner_user_id=user_id,
        category=interp["category"], need_text=clean,
        requirements=interp["requirements"],
        idempotency_key=uuid.uuid4().hex, expires_at=expires,
        quantity_requested=quantity)
    confirmed = need_engine.confirm_request(conn, req_id, user_id)
    matcher.trigger_matching(conn, "request_saved", "request", req_id)
    matches = match_cards(conn, req_id)

    title = need_engine.request_title(clean)
    profile = _profile(conn, twin_id)
    replies = [f"Got it — searching for {title}."]
    context = _profile_line(profile)
    if context:
        replies[0] += " " + context
    question_text = _material_question(interp["category"],
                                       interp["requirements"])
    if question_text:
        replies.append(question_text)
        _say(conn, user_id, twin_id, replies[0])
        _say(conn, user_id, twin_id, question_text, kind="question",
             ref_type="request", ref_id=req_id)
    else:
        _say(conn, user_id, twin_id, replies[0])
    if matches:
        results = (f"I found {len(matches)} relevant result"
                   f"{'s' if len(matches) != 1 else ''}:")
    else:
        results = ("Nothing suitable yet — I'll keep looking"
                   f" {_deadline_label(confirmed['expires_at'])} and"
                   " notify you when something appears.")
    replies.append(results)
    _say(conn, user_id, twin_id, results, kind="results",
         ref_type="request", ref_id=req_id)
    conn.commit()
    return {"need_id": req_id, "matches": matches, "replies": replies}
