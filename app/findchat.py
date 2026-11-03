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
    add_twin_skills,
    create_twin_chat,
    get_opportunity,
    get_twin,
    list_matches_for_search,
    list_requests_for_user,
    list_twin_chats,
    resolve_twin_chat,
    twin_profile,
)
from .intent import interpret_need
from .request_parse import parse_need

#: Default search window when the owner states no duration (1 week).
DEFAULT_SEARCH_MINUTES = 10080

MAX_MESSAGE = 2000

_ADD_SKILLS_RE = re.compile(r"\badd\s+(.+?)\s+to\s+my\s+skills?\b", re.I)
_MY_SKILLS_RE = re.compile(r"\bmy\s+skills?\s+(?:are|is|:)\s*(.+)", re.I)
_I_KNOW_RE = re.compile(r"\bi\s+(?:also\s+)?know\s+(.+)", re.I)
_I_KNOW_ARTICLE = re.compile(r"\b(a|an|the|someone|somebody)\b", re.I)
_SKILL_STOP = {"a", "an", "the", "my", "skills", "skill", "and", "please", "none", "nothing", "nope", "no"}
_SEEKING_WORK_RE = re.compile(r"\b(jobs?|work|employment|career|gig|opportunit\w*|position|vacanc\w*|interview|match\s+me|match\s+my\s+skills)\b", re.I)

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
    return twin_profile(twin) if twin is not None else {}


def parse_skill_list(text: str) -> list[str]:
    found = []
    for chunk in re.split(r",|;|\band\b|&|\+", text or ""):
        word = " ".join(chunk.split()).strip().strip(".")[:60]
        if not word or word.lower() in _SKILL_STOP or len(word.split()) > 4:
            continue
        if word.lower() not in {x.lower() for x in found}:
            found.append(word)
    return found[:10]


def explicit_skill_statement(text: str) -> list[str] | None:
    clean = (text or "").strip()
    match = _ADD_SKILLS_RE.search(clean) or _MY_SKILLS_RE.search(clean)
    if match:
        return parse_skill_list(match.group(1))
    known = _I_KNOW_RE.search(clean)
    if known and not _I_KNOW_ARTICLE.search(known.group(1)):
        return parse_skill_list(known.group(1))
    return None


def uses_personal_skills(category: str, text: str) -> bool:
    return (category or "").lower() == "job" or bool(_SEEKING_WORK_RE.search(text or ""))


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


def _material_question(category: str, requirements: dict, *,
                       twin_skills: list | None = None,
                       need_text: str = "") -> tuple[str, str]:
    if category == "transport":
        if not requirements.get("origin") and not requirements.get("destination"):
            return ("Where should the trip start and end? A maximum budget helps too, if you have one.", "request")
        if not requirements.get("origin"):
            return ("Where should the trip start?", "request")
        if not requirements.get("destination"):
            return ("Where are you headed?", "request")
    if uses_personal_skills(category, need_text) and not (twin_skills or []) and not requirements.get("skills"):
        return ("You haven't shared your skills with me yet. What skills should I search with?", "request-skills")
    return "", "request"


def _deadline_label(expires_at: str | None) -> str:
    if not expires_at:
        return "until you cancel it"
    return "until " + expires_at[:10]


def _is_answer(text: str, twin_messages: list) -> Row | None:
    """Return the newest unanswered question this message answers."""
    pending = next(
        (m for m in reversed(twin_messages)
         if m["kind"] == "question" and m["ref_type"] in ("request", "request-skills")),
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


def _answer_skills_question(conn: Connection, user_id: int, twin_id: int,
                            text: str, pending: Row) -> dict:
    req = need_engine.get_request(conn, pending["ref_id"])
    if req is None or req["owner_user_id"] != user_id:
        raise FindChatError("That search is gone; please describe your need again.")
    skills = explicit_skill_statement(text) or parse_skill_list(text)
    if not skills:
        reply = "I could not pick out skills; try a short list like Python, SQL."
        _say(conn, user_id, twin_id, reply, kind="question",
             ref_type="request-skills", ref_id=req["id"])
        return {"need_id": req["id"], "matches": [], "replies": [reply]}
    stored = add_twin_skills(conn, twin_id, skills)
    merged = dict(need_engine.request_requirements(req))
    have = {v.lower() for v in merged.get("skills", []) if isinstance(v, str)}
    merged["skills"] = list(merged.get("skills", [])) + [v for v in stored if v.lower() not in have]
    need_engine.update_need(conn, req["id"], user_id, requirements=merged)
    resolve_twin_chat(conn, pending["id"], user_id)
    matcher.trigger_matching(conn, "request_saved", "request", req["id"])
    matches = match_cards(conn, req["id"])
    reply = f"Thanks — I saved your skills ({', '.join(stored)})."
    if matches:
        reply += f" I found {len(matches)} relevant result{'s' if len(matches) != 1 else ''}."
    else:
        reply += " Nothing suitable yet; I will keep looking " + _deadline_label(req["expires_at"]) + "."
    _say(conn, user_id, twin_id, reply, kind="results", ref_type="request", ref_id=req["id"])
    return {"need_id": req["id"], "matches": matches, "replies": [reply]}


def _answer_question(conn: Connection, user_id: int, twin_id: int,
                     text: str, pending: Row) -> dict:
    """Merge a short answer into the need that asked the question."""
    if pending["ref_type"] == "request-skills":
        return _answer_skills_question(conn, user_id, twin_id, text, pending)
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
    if ("origin" in merged) != ("destination" in merged):
        from .request_parse import KNOWN_PLACES
        place = " ".join((text or "").split()).strip(" .")
        canonical = next((p for p in KNOWN_PLACES if p.lower() == place.lower()), None)
        if canonical is None and re.fullmatch(r"[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*)*", place) and len(place.split()) <= 3:
            canonical = place
        if canonical:
            missing = "destination" if "origin" in merged else "origin"
            merged[missing] = canonical
            added.append(missing)
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


def _pending_twin_update(conn: Connection, user_id: int):
    return conn.execute(
        "SELECT * FROM twin_chats WHERE user_id = ? AND role = 'twin'"
        " AND kind = 'question' AND ref_type = 'twin-update'"
        " ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()


def _twin_update_turn(conn: Connection, user_id: int, twin_id: int,
                      text: str, pending) -> dict:
    from . import twin_update_nl as updates

    action_id = pending["ref_id"]
    row = conn.execute(
        "SELECT * FROM twin_chats WHERE id = ? AND user_id = ?"
        " AND role = 'system' AND kind = 'twin-action'",
        (action_id, user_id)).fetchone()
    if row is None:
        reply = "That edit preview expired. Please state the change again."
        _say(conn, user_id, twin_id, reply)
        conn.commit()
        return {"need_id": None, "matches": [], "replies": [reply]}
    action = _json.loads(row["body"] or "{}")
    answer = text.strip().lower().strip(".! ")
    if answer in ("no", "cancel", "cancel it", "don't", "dont"):
        action["status"] = "cancelled"
        reply = "Okay, I cancelled that profile edit. Nothing was changed."
    elif answer in ("yes", "confirm", "save", "do it", "apply"):
        if action.get("status") != "pending":
            reply = "That profile edit is no longer pending; nothing was changed."
        else:
            current_before = updates.scoped_snapshot(get_twin(conn, twin_id), set(action["fields"]))
            if current_before != action.get("expected_before", {}):
                action["status"] = "stale"
                reply = "Your twin changed after that preview. Nothing was saved; please state the update again so I can show a fresh preview."
                result = None
            else:
                result = updates.apply(conn, user_id, twin_id, action["ops"], commit=False)
            if result is not None and (result["errors"] or not result["changes"]):
                action["status"] = "failed"
                reply = "I could not safely apply that edit: " + " ".join(result["errors"] or ["there is no change to save"])
            elif result is not None:
                current = get_twin(conn, twin_id)
                action.update(status="applied",
                              before=updates.scoped_snapshot_from_full(result["undone"], action["fields"]),
                              after=updates.scoped_snapshot(current, set(action["fields"])))
                reply = "Saved: " + updates.describe({"changes": result["changes"]}) + ". Say 'undo update' to reverse these fields."
    else:
        reply = "Please reply 'confirm' to save this profile edit or 'cancel' to leave your twin unchanged."
        _say(conn, user_id, twin_id, reply)
        conn.commit()
        return {"need_id": None, "matches": [], "replies": [reply]}
    conn.execute("UPDATE twin_chats SET body = ? WHERE id = ?",
                 (_json.dumps(action), action_id))
    conn.execute("UPDATE twin_chats SET kind = 'answered' WHERE id = ?",
                 (pending["id"],))
    _say(conn, user_id, twin_id, reply)
    conn.commit()
    return {"need_id": None, "matches": [], "replies": [reply]}


def _handle_twin_text(conn: Connection, user_id: int, twin_id: int,
                      clean: str) -> dict | None:
    from . import twin_update_nl as updates
    pending = _pending_twin_update(conn, user_id)
    if pending is not None:
        return _twin_update_turn(conn, user_id, twin_id, clean, pending)
    # The established explicit skill flow is additive and remains intact.
    # It cannot overwrite the existing personal skill list.
    if not _NEED_VERB.search(clean) and explicit_skill_statement(clean) is not None:
        return None

    teach = re.match(r"^(?:please\s+)?teach\s+(?:my\s+)?twin\s+(?:that\s+)?(.+)$", clean, re.I)
    if teach:
        from .memories import MemoryError, create_memory
        try:
            mem_id = create_memory(conn, twin_id=twin_id,
                                   content=teach.group(1).strip(" ."),
                                   source="manual", category="general")
            reply = ("Added to pending memories for your review at /memories."
                     if mem_id else "That fact is already in your memory list; nothing was overwritten.")
        except MemoryError as exc:
            reply = "I could not add that memory: " + str(exc)
        _say(conn, user_id, twin_id, reply)
        conn.commit()
        return {"need_id": None, "matches": [], "replies": [reply]}

    if re.fullmatch(r"(?:undo|revert)(?: my)?(?: last)?(?: twin)? update", clean.strip(".! "), re.I):
        rows = conn.execute(
            "SELECT * FROM twin_chats WHERE user_id = ? AND role = 'system'"
            " AND kind = 'twin-action' ORDER BY id DESC", (user_id,)).fetchall()
        action_row = next((r for r in rows if _json.loads(r["body"] or "{}").get("status") == "applied"), None)
        if action_row is None:
            reply = "There is no saved twin update available to undo."
        else:
            action = _json.loads(action_row["body"] or "{}")
            result = updates.undo(conn, user_id, twin_id, action, commit=False)
            action["status"] = "undone"
            conn.execute("UPDATE twin_chats SET body = ? WHERE id = ?",
                         (_json.dumps(action), action_row["id"]))
            reply = ("Undid fields: " + ", ".join(result["restored"])
                     if result["restored"] else
                     "Nothing was changed because those fields have since changed.")
            if result["conflicts"]:
                reply += " Kept later edits to: " + ", ".join(result["conflicts"]) + "."
        _say(conn, user_id, twin_id, reply)
        conn.commit()
        return {"need_id": None, "matches": [], "replies": [reply]}

    if not updates.is_update_command(clean):
        return None
    parsed = updates.parse_update(clean)
    report = None
    if parsed["errors"] or not parsed["ops"]:
        reply = "I could not identify a safe profile change. " + " ".join(parsed["errors"] or ["Specify a field, such as: set my headline to AI developer."])
    else:
        report = updates.preview(conn, twin_id, parsed["ops"])
        if report["errors"]:
            reply = "I need clarification before changing anything: " + " ".join(report["errors"])
        elif not report["changes"]:
            reply = "That would not change your twin, so nothing was saved."
        else:
            fields = sorted({op["field"] for op, _ in report["applied"]})
            action = {"status": "pending", "ops": parsed["ops"], "fields": fields,
                      "expected_before": updates.scoped_snapshot(get_twin(conn, twin_id), set(fields))}
            aid = create_twin_chat(conn, user_id=user_id, twin_id=twin_id,
                                   role="system", body=_json.dumps(action),
                                   kind="twin-action", ref_type="twin-update")
            reply = "Proposed profile edit: " + updates.describe(report) + ". Reply 'confirm' to save or 'cancel' to leave it unchanged."
            _say(conn, user_id, twin_id, reply, kind="question",
                 ref_type="twin-update", ref_id=aid)
            conn.commit()
            return {"need_id": None, "matches": [], "replies": [reply]}
    _say(conn, user_id, twin_id, reply)
    conn.commit()
    return {"need_id": None, "matches": [], "replies": [reply]}


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

    twin_text = _handle_twin_text(conn, user_id, twin_id, clean)
    if twin_text is not None:
        return twin_text

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

    if not has_need_verb:
        stated_only = explicit_skill_statement(clean)
        if stated_only is not None:
            if not stated_only:
                reply = "I could not pick out skills; try 'add Python, SQL to my skills'."
            else:
                stored = add_twin_skills(conn, twin_id, stated_only)
                reply = "Got it — your skills are now: " + ", ".join(stored) + ". I will use them in job searches."
            _say(conn, user_id, twin_id, reply)
            conn.commit()
            return {"need_id": None, "matches": [], "replies": [reply]}

    stated = explicit_skill_statement(clean)
    if stated:
        add_twin_skills(conn, twin_id, stated)
    parsed = parse_need(clean)
    interp = interpret_need(clean, conn=conn, user_id=user_id)
    minutes = parsed["deadline_minutes"] or DEFAULT_SEARCH_MINUTES
    expires = matcher.plus_minutes(matcher.now_stamp(), minutes)
    quantity = parsed["quantity"] or 1
    profile = _profile(conn, twin_id)
    requirements = dict(interp["requirements"])
    stored_skills = [x for x in profile.get("skills", []) if isinstance(x, str)]
    search_skills = []
    if uses_personal_skills(interp["category"], clean):
        seen = set()
        for skill in list(requirements.get("skills", [])) + stored_skills:
            if isinstance(skill, str) and skill.strip() and skill.lower() not in seen:
                seen.add(skill.lower())
                search_skills.append(skill.strip())
        if search_skills:
            requirements["skills"] = search_skills
        if not requirements.get("origin") and not requirements.get("destination"):
            requirements["destination"] = (profile.get("location") or "").strip() or "Thimphu"
    req_id, _ = need_engine.create_need(
        conn, twin_id=twin_id, owner_user_id=user_id,
        category=interp["category"], need_text=clean,
        requirements=requirements,
        idempotency_key=uuid.uuid4().hex, expires_at=expires,
        quantity_requested=quantity)
    confirmed = need_engine.confirm_request(conn, req_id, user_id)
    matcher.trigger_matching(conn, "request_saved", "request", req_id)
    matches = match_cards(conn, req_id)

    title = need_engine.request_title(clean)
    replies = [f"Got it — searching for {title}."]
    context = _profile_line(profile)
    if context:
        replies[0] += " " + context
    if search_skills:
        replies[0] += " Searching with your skills: " + ", ".join(search_skills[:8]) + "."
    if stated:
        replies[0] += " I saved your skills (" + ", ".join(stated) + ")."
    question_text, question_ref = _material_question(
        interp["category"], requirements, twin_skills=stored_skills,
        need_text=clean)
    if question_text:
        replies.append(question_text)
        _say(conn, user_id, twin_id, replies[0])
        _say(conn, user_id, twin_id, question_text, kind="question",
             ref_type=question_ref, ref_id=req_id)
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
