"""Reusable twin-to-twin conversation engine.

Generic lifecycle (NOT recruitment-specific): two twins, ordered history,
turn limits, pause/resume/withdraw, owner notes, audit events. Recruitment
is one `purpose` value that additionally links an opportunity and can
produce a deterministic recommendation. The twin NEVER makes final
decisions -- owners record those explicitly.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from sqlite3 import Connection, Row
from typing import Any

from . import budget, privacy
from . import requests as need_engine
from .budget import BudgetExhausted
from .db import get_opportunity, get_request, get_twin, insert_returning_id
from .meta_client import MetaAPIError, MetaNotConfigured


def _utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
from .providers import MockProvider, get_provider

PURPOSES = ("open-chat", "recruitment", "service")
STATUSES = ("active", "paused", "closed")
DECISIONS = ("interested", "not-interested")
DEFAULT_MAX_TURNS = 12
HISTORY_WINDOW = 6


class ConversationError(Exception):
    """User-facing engine error (permission, state, or limit problem)."""


@dataclass
class TwinContext:
    twin_name: str
    owner_name: str
    persona: str
    tone: str
    public_view: dict[str, Any] = field(default_factory=dict)
    memories: list[str] = field(default_factory=list)
    history: list[tuple[str, str]] = field(default_factory=list)  # ("me"/"peer", body)
    peer_name: str = "there"
    purpose: str = "open-chat"
    opportunity: dict[str, Any] | None = None
    # Side-appropriate request facts. The SEEKER side sees full requirements;
    # the PROVIDER side sees the budget-stripped peer summary. Providers never
    # construct prompts containing the seeker's budget ceiling.
    request_summary: dict[str, Any] | None = None


def _parse(raw: Any, fallback):
    try:
        parsed = json.loads(raw or "")
    except (ValueError, TypeError):
        return fallback
    return parsed if isinstance(parsed, type(fallback)) else fallback


def _twin_side(conv: Row, twin_id: int) -> str | None:
    if twin_id == conv["twin_a_id"]:
        return "a"
    if twin_id == conv["twin_b_id"]:
        return "b"
    return None


def owner_of_twin(conn: Connection, twin_id: int) -> int | None:
    twin = get_twin(conn, twin_id)
    return twin["user_id"] if twin else None


def is_participant_owner(conn: Connection, conv: Row, user_id: int | None) -> bool:
    if user_id is None:
        return False
    return user_id in (
        owner_of_twin(conn, conv["twin_a_id"]),
        owner_of_twin(conn, conv["twin_b_id"]),
    )


def find_open_request_conversation(conn: Connection, twin_a_id: int,
                                     twin_b_id: int, request_id: int) -> Row | None:
    """Existing non-closed chat for one request contact (idempotency probe).

    Direction-agnostic: a seeker-initiated contact and a later
    provider-initiated contact about the same need reuse the one open
    chat instead of forking duplicates.
    """
    return conn.execute(
        "SELECT * FROM conversations"
        " WHERE ((twin_a_id = ? AND twin_b_id = ?)"
        " OR (twin_a_id = ? AND twin_b_id = ?))"
        " AND request_id = ? AND status != 'closed' ORDER BY id LIMIT 1",
        (twin_a_id, twin_b_id, twin_b_id, twin_a_id, request_id),
    ).fetchone()


def create_conversation(
    conn: Connection,
    *,
    twin_a_id: int,
    twin_b_id: int,
    created_by_user_id: int,
    opportunity_id: int | None = None,
    request_id: int | None = None,
    purpose: str = "open-chat",
    max_turns: int = DEFAULT_MAX_TURNS,
    is_demo: bool = False,
) -> int:
    if purpose not in PURPOSES:
        raise ConversationError(f"Unknown purpose: {purpose}")
    if twin_a_id == twin_b_id:
        raise ConversationError("A twin cannot converse with itself.")
    twin_a, twin_b = get_twin(conn, twin_a_id), get_twin(conn, twin_b_id)
    if twin_a is None or twin_b is None:
        raise ConversationError("Both twins must exist.")
    if created_by_user_id != twin_a["user_id"]:
        raise ConversationError("A conversation must be started by the initiating twin's owner.")
    for twin in (twin_a, twin_b):
        if twin["user_id"] != created_by_user_id and not twin["is_public"]:
            raise ConversationError("That twin is private and cannot be contacted.")
    if opportunity_id is not None and get_opportunity(conn, opportunity_id) is None:
        raise ConversationError("Linked opportunity does not exist.")
    if request_id is not None:
        req = get_request(conn, request_id)
        if req is None:
            raise ConversationError("Linked request does not exist.")
        if req["twin_id"] not in (twin_a_id, twin_b_id):
            raise ConversationError(
                "Request does not belong to either twin.")
        if not need_engine.is_shared(req):
            raise ConversationError(
                "That request is private or withdrawn.")
    max_turns = max(2, min(30, int(max_turns)))
    conv_id = insert_returning_id(
        conn,
        "INSERT INTO conversations (twin_a_id, twin_b_id, opportunity_id, request_id,"
        " purpose, status, turn, max_turns, is_demo, created_by_user_id)"
        " VALUES (?, ?, ?, ?, ?, 'active', 'a', ?, ?, ?)",
        (twin_a_id, twin_b_id, opportunity_id, request_id, purpose, max_turns,
         1 if is_demo else 0, created_by_user_id),
    )
    conn.execute(
        "INSERT INTO messages (conv_id, sender, body, provider) VALUES (?, 'system', ?, 'mock')",
        (conv_id, f"Conversation started between {twin_a['display_name']} and "
                  f"{twin_b['display_name']} ({purpose})."),
    )
    audit(conn, conv_id, "created", created_by_user_id, f"purpose={purpose}")
    conn.commit()
    return conv_id


def get_or_create_request_conversation(
    conn: Connection, *, twin_a_id: int, twin_b_id: int,
    created_by_user_id: int, request_id: int,
    opportunity_id: int | None = None,
    purpose: str = "service", max_turns: int = DEFAULT_MAX_TURNS,
) -> tuple[int, bool]:
    """Contact idempotency: reuse the open chat for one request, else create.

    twin_a is always the initiator's twin (enforced by
    create_conversation); the linked request may belong to either
    side, so providers can answer a discovered need. opportunity_id
    records which offering was contacted, when the contact started
    from one.
    """
    existing = find_open_request_conversation(conn, twin_a_id, twin_b_id, request_id)
    if existing is not None:
        audit(conn, existing["id"], "contact-reused", created_by_user_id,
              f"request={request_id}")
        conn.commit()
        return existing["id"], False
    conv_id = create_conversation(
        conn, twin_a_id=twin_a_id, twin_b_id=twin_b_id,
        created_by_user_id=created_by_user_id, request_id=request_id,
        opportunity_id=opportunity_id, purpose=purpose, max_turns=max_turns)
    need_engine.mark_chatting(conn, request_id)
    return conv_id, True


def get_conversation(conn: Connection, conv_id: int) -> Row | None:
    return conn.execute(
        "SELECT c.*, ta.display_name AS name_a, ta.avatar_seed AS seed_a,"
        " tb.display_name AS name_b, tb.avatar_seed AS seed_b,"
        " ua.id AS owner_a_id, ub.id AS owner_b_id,"
        " ua.name AS owner_a_name, ub.name AS owner_b_name,"
        " o.title AS opp_title, r.category AS req_category"
        " FROM conversations c"
        " JOIN twins ta ON ta.id = c.twin_a_id"
        " JOIN twins tb ON tb.id = c.twin_b_id"
        " JOIN users ua ON ua.id = ta.user_id"
        " JOIN users ub ON ub.id = tb.user_id"
        " LEFT JOIN opportunities o ON o.id = c.opportunity_id"
        " LEFT JOIN requests r ON r.id = c.request_id"
        " WHERE c.id = ?",
        (conv_id,),
    ).fetchone()


def list_for_user(conn: Connection, user_id: int) -> list[Row]:
    return list(
        conn.execute(
            "SELECT c.*, ta.display_name AS name_a, tb.display_name AS name_b,"
            " ta.user_id AS owner_a_id, tb.user_id AS owner_b_id,"
            " o.title AS opp_title FROM conversations c"
            " JOIN twins ta ON ta.id = c.twin_a_id"
            " JOIN twins tb ON tb.id = c.twin_b_id"
            " LEFT JOIN opportunities o ON o.id = c.opportunity_id"
            " WHERE ta.user_id = ? OR tb.user_id = ?"
            " ORDER BY c.updated_at DESC, c.id DESC",
            (user_id, user_id),
        )
    )


def list_demos(conn: Connection) -> list[Row]:
    return list(
        conn.execute(
            "SELECT c.*, ta.display_name AS name_a, tb.display_name AS name_b,"
            " o.title AS opp_title FROM conversations c"
            " JOIN twins ta ON ta.id = c.twin_a_id"
            " JOIN twins tb ON tb.id = c.twin_b_id"
            " LEFT JOIN opportunities o ON o.id = c.opportunity_id"
            " WHERE c.is_demo = 1 ORDER BY c.updated_at DESC, c.id DESC"
        )
    )


def list_messages(conn: Connection, conv_id: int, since_id: int = 0, limit: int = 200) -> list[Row]:
    return list(
        conn.execute(
            "SELECT * FROM messages WHERE conv_id = ? AND id > ?"
            " ORDER BY id ASC LIMIT ?",
            (conv_id, max(0, since_id), max(1, min(500, limit))),
        )
    )


def twin_message_count(conn: Connection, conv_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE conv_id = ? AND sender IN ('a', 'b')",
        (conv_id,),
    ).fetchone()
    return row["n"]


def audit(conn: Connection, conv_id: int | None, action: str,
          actor_user_id: int | None, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_events (conv_id, action, actor_user_id, detail)"
        " VALUES (?, ?, ?, ?)",
        (conv_id, action, actor_user_id, detail[:500]),
    )


def build_context(conn: Connection, conv: Row, speaker_side: str,
                  memories: list[str] | None = None) -> TwinContext:
    speaker_id = conv["twin_a_id"] if speaker_side == "a" else conv["twin_b_id"]
    speaker = get_twin(conn, speaker_id)
    peer_name = conv["name_b"] if speaker_side == "a" else conv["name_a"]
    history: list[tuple[str, str]] = []
    for msg in list_messages(conn, conv["id"])[-HISTORY_WINDOW:]:
        if msg["sender"] == "system":
            continue
        if msg["sender"] in ("a", "b"):
            who = "me" if msg["sender"] == speaker_side else "peer"
        else:  # owner notes are visible context for both twins
            who = "peer"
        history.append((who, msg["body"]))
    opp = None
    if conv["opportunity_id"]:
        row = get_opportunity(conn, conv["opportunity_id"])
        if row:
            opp = {"title": row["title"], "kind": row["kind"],
                   "required": _parse(row["required_skills_json"], []),
                   "location": row["location"]}
    request_summary = None
    if conv["request_id"]:
        from .matching import provider_summary_for_peer

        req = get_request(conn, conv["request_id"])
        if req is not None:
            full = need_engine.request_requirements(req)
            is_seeker_side = speaker_id == req["twin_id"]
            visible = full if is_seeker_side else provider_summary_for_peer(full)
            request_summary = {"category": req["category"], **visible}
    return TwinContext(
        twin_name=speaker["display_name"],
        owner_name=speaker["owner_name"],
        persona=speaker["persona"],
        tone=speaker["tone"],
        public_view=privacy.twin_public_view(speaker),
        memories=list(memories or []),
        history=history,
        peer_name=peer_name,
        purpose=conv["purpose"],
        opportunity=opp,
        request_summary=request_summary,
    )


def speaker_blocked_reason(conn: Connection, conv: Row, speaker_side: str) -> str | None:
    """Owner-control gate: returns why the speaker may not auto-reply, or None.

    When a twin's effective conditions set auto_answer=false, each twin turn
    needs a fresh guiding note from its owner since the last twin message.
    """
    speaker_id = conv["twin_a_id"] if speaker_side == "a" else conv["twin_b_id"]
    speaker = get_twin(conn, speaker_id)
    req = get_request(conn, conv["request_id"]) if conv["request_id"] else None
    if need_engine.effective_conditions(speaker, req).get("auto_answer", True):
        return None
    owner_sender = f"owner_{speaker_side}"
    guided = False
    for msg in list_messages(conn, conv["id"]):
        if msg["sender"] in ("a", "b"):
            guided = False
        elif msg["sender"] == owner_sender:
            guided = True
    if guided:
        return None
    return (f"{speaker['display_name']} needs an owner note before replying "
            "(auto-answer is off).")


def conversation_requires_approval(conn: Connection, conv: Row) -> bool:
    """True when either side demands per-turn owner approval (blocks auto-play)."""
    req = get_request(conn, conv["request_id"]) if conv["request_id"] else None
    for twin_id in (conv["twin_a_id"], conv["twin_b_id"]):
        twin = get_twin(conn, twin_id)
        if twin is not None and need_engine.effective_conditions(twin, req).get(
                "require_approval", False):
            return True
    return False


def step_conversation(conn: Connection, conv_id: int, actor_user_id: int | None,
                      memories: list[str] | None = None,
                      provider=None,
                      max_tokens: int | None = None) -> dict[str, Any]:
    """Advance one twin turn. Returns the new message plus status info."""
    conv = get_conversation(conn, conv_id)
    if conv is None:
        raise ConversationError("Conversation not found.")
    if not is_participant_owner(conn, conv, actor_user_id):
        raise ConversationError("Only a participant's owner can advance this chat.")
    if conv["status"] == "paused":
        raise ConversationError("Conversation is paused. Resume it first.")
    if conv["status"] == "closed":
        raise ConversationError("Conversation is closed.")
    if twin_message_count(conn, conv_id) >= conv["max_turns"]:
        raise ConversationError("Turn limit reached.")
    blocked = speaker_blocked_reason(conn, conv, conv["turn"])
    if blocked is not None:
        raise ConversationError(blocked)

    provider = provider or get_provider()
    ctx = build_context(conn, conv, conv["turn"], memories)
    try:
        result = budget.guarded_generate(
            conn, provider, ctx, conv_id=conv_id, user_id=actor_user_id,
            max_tokens=max_tokens)
        used_provider = provider.name
    except (BudgetExhausted, MetaAPIError, MetaNotConfigured) as exc:
        if provider.name == "mock":
            raise ConversationError(f"Reply failed: {exc}")
        if isinstance(exc, MetaAPIError):
            # A request was attempted: it may have been billed, so it must
            # leave a row (mirrors intent.py / router.py). Known usage
            # keeps its real cost; unknown usage is flagged failed_unknown
            # and priced at the conservative estimate, so budget checks
            # count it instead of treating it as free.
            if exc.usage_known:
                budget.record_usage(
                    conn, provider=provider.name,
                    model=getattr(provider, "model", ""),
                    conv_id=conv_id, user_id=actor_user_id,
                    tokens_in=exc.tokens_in, tokens_out=exc.tokens_out,
                    status="failed_known", notes=str(exc))
            else:
                budget.record_usage(
                    conn, provider=provider.name,
                    model=getattr(provider, "model", ""),
                    conv_id=conv_id, user_id=actor_user_id,
                    tokens_in=0, tokens_out=0,
                    status="failed_unknown", notes=str(exc))
        # Graceful, transparent degradation: free mock + visible system notice.
        conn.execute(
            "INSERT INTO messages (conv_id, sender, body, provider)"
            " VALUES (?, 'system', ?, 'mock')",
            (conv_id, f"Meta inference unavailable ({exc})."
                      " Continuing with deterministic mock replies."),
        )
        audit(conn, conv_id, "budget-fallback", actor_user_id, provider.name)
        mock = MockProvider()
        result = budget.guarded_generate(
            conn, mock, ctx, conv_id=conv_id, user_id=actor_user_id)
        used_provider = "mock"
    msg_id = insert_returning_id(
        conn,
        "INSERT INTO messages (conv_id, sender, body, provider, tokens_in, tokens_out)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (conv_id, conv["turn"], result.text[:2000], used_provider,
         result.tokens_in, result.tokens_out),
    )
    next_turn = "b" if conv["turn"] == "a" else "a"
    auto_closed = twin_message_count(conn, conv_id) >= conv["max_turns"]
    new_status = "closed" if auto_closed else "active"
    conn.execute(
        "UPDATE conversations SET turn = ?, status = ?, updated_at = ?"
        " WHERE id = ?",
        (next_turn, new_status, _utcnow_iso(), conv_id),
    )
    audit(conn, conv_id, "step", actor_user_id, f"sender={conv['turn']} via={provider.name}")
    recommendation = None
    if auto_closed:
        conn.execute(
            "INSERT INTO messages (conv_id, sender, body, provider)"
            " VALUES (?, 'system', ?, 'mock')",
            (conv_id, f"Turn limit reached ({conv['max_turns']} twin messages). Conversation closed."),
        )
        audit(conn, conv_id, "auto-closed", None, "turn limit")
        # NOTE (hackathon compliance): recruitment close-out used to
        # append automated candidate verdicts and interview questions.
        # Removed: twins coordinate but never assess applicants.
        if conv["purpose"] == "service" and conv["request_id"]:
            recommendation = generate_service_summary(conn, conv_id)
            conn.execute(
                "INSERT INTO messages (conv_id, sender, body, provider)"
                " VALUES (?, 'system', ?, 'mock')",
                (conv_id, recommendation),
            )
    conn.commit()
    msg = conn.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)).fetchone()
    return {"message": dict(msg), "status": new_status, "auto_closed": auto_closed,
            "recommendation": recommendation}


def _require_owner(conn: Connection, conv_id: int, user_id: int | None) -> Row:
    conv = get_conversation(conn, conv_id)
    if conv is None:
        raise ConversationError("Conversation not found.")
    if not is_participant_owner(conn, conv, user_id):
        raise ConversationError("Only a participant's owner can do that.")
    return conv


def pause_conversation(conn: Connection, conv_id: int, user_id: int | None) -> None:
    conv = _require_owner(conn, conv_id, user_id)
    if conv["status"] != "active":
        raise ConversationError("Only an active conversation can be paused.")
    conn.execute("UPDATE conversations SET status = 'paused' WHERE id = ?", (conv_id,))
    audit(conn, conv_id, "paused", user_id, "")
    conn.commit()


def resume_conversation(conn: Connection, conv_id: int, user_id: int | None) -> None:
    conv = _require_owner(conn, conv_id, user_id)
    if conv["status"] != "paused":
        raise ConversationError("Only a paused conversation can be resumed.")
    conn.execute("UPDATE conversations SET status = 'active' WHERE id = ?", (conv_id,))
    audit(conn, conv_id, "resumed", user_id, "")
    conn.commit()


def close_conversation(conn: Connection, conv_id: int, user_id: int | None, reason: str = "") -> None:
    conv = _require_owner(conn, conv_id, user_id)
    if conv["status"] == "closed":
        raise ConversationError("Conversation is already closed.")
    conn.execute("UPDATE conversations SET status = 'closed' WHERE id = ?", (conv_id,))
    conn.execute(
        "INSERT INTO messages (conv_id, sender, body, provider) VALUES (?, 'system', ?, 'mock')",
        (conv_id, f"Conversation closed by owner.{(' ' + reason) if reason else ''}"),
    )
    audit(conn, conv_id, "closed", user_id, reason)
    conn.commit()


def owner_note(conn: Connection, conv_id: int, user_id: int | None, text: str) -> int:
    conv = _require_owner(conn, conv_id, user_id)
    if conv["status"] == "closed":
        raise ConversationError("Conversation is closed.")
    text = text.strip()
    if not text:
        raise ConversationError("Note cannot be empty.")
    side = "a" if user_id == conv["owner_a_id"] else "b"
    note_id = insert_returning_id(
        conn,
        "INSERT INTO messages (conv_id, sender, body, provider) VALUES (?, ?, ?, 'owner')",
        (conv_id, f"owner_{side}", text[:2000]),
    )
    audit(conn, conv_id, "owner-note", user_id, "")
    conn.commit()
    return note_id


def set_decision(conn: Connection, conv_id: int, user_id: int | None, decision: str) -> None:
    if decision not in DECISIONS:
        raise ConversationError("Decision must be 'interested' or 'not-interested'.")
    conv = _require_owner(conn, conv_id, user_id)
    column = "decision_a" if user_id == conv["owner_a_id"] else "decision_b"
    conn.execute(f"UPDATE conversations SET {column} = ? WHERE id = ?", (decision, conv_id))
    audit(conn, conv_id, "decision", user_id, f"{column}={decision}")
    conn.commit()


def generate_service_summary(conn: Connection, conv_id: int) -> str:
    """Deterministic service-chat summary. Category-neutral, no AI needed."""
    conv = get_conversation(conn, conv_id)
    if conv is None:
        raise ConversationError("Conversation not found.")
    req = get_request(conn, conv["request_id"]) if conv["request_id"] else None
    if req is None:
        raise ConversationError("Summary needs a linked request.")
    turns = twin_message_count(conn, conv_id)
    questions = sum(1 for m in list_messages(conn, conv_id) if "?" in m["body"])
    need = (req["need_text"] or "").strip()
    if len(need) > 180:
        need = need[:177] + "..."
    lines = [
        f"Service summary ({conv['req_category'] or 'general'}, deterministic):",
        f"- Request: {need}",
        f"- Between: {conv['name_a']} ({conv['owner_a_name']})"
        f" and {conv['name_b']} ({conv['owner_b_name']})",
        f"- Exchange depth: {turns} twin messages, {questions} questions asked",
        "- No offer was accepted and no payment was made: twins cannot commit.",
        "Next step: each owner reviews the chat and records a decision.",
    ]
    return "\n".join(lines)


# NOTE (hackathon compliance): the automated recruitment verdict
# (generate_recommendation: RECOMMENDED/PROMISING/WEAK candidate
# assessments) was removed. Twins coordinate about opportunities but
# never score, rank, or judge applicants. Owner-side discovery lives
# in app/matching.py; generate_service_summary above stays neutral.
