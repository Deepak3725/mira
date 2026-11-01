"""Twin intelligence: answers from owner-approved knowledge only.

Read-only by construction (SELECTs only; a test proves no writes).
Twins answer from their own owner's approved memories, cite sources,
say "I don't know yet" when nothing matches, and can ask the owner
for input. Twins never hire, buy, pay, sign, or commit: this module
exposes no such action, and approvals stay with the owner.
"""

from __future__ import annotations

from . import conversations as engine
from . import memories
from .db import insert_returning_id


def answer_question(conn, twin_id: int, question: str,
                    limit: int = 3) -> dict:
    """Answer an owner question from approved memories (deterministic)."""
    # NOTE: relevant_memories returns contents only; re-derive rows for
    # sources by matching content (contents are unique per twin: the
    # dedupe in create_memory guarantees it).
    texts = memories.relevant_memories(conn, twin_id, question, limit)
    rows = {r["content"]: r for r in
            memories.list_memories(conn, twin_id, "approved")}
    matched = [rows[t] for t in texts if t in rows]
    styles = [r for r in memories.list_memories(conn, twin_id, "approved")
              if r["category"] == "style"][:2]
    if not matched:
        return {"answer": ("I don't know that yet from your approved "
                           "memories. Import or approve knowledge about "
                           "it first, and I'll answer next time."),
                "sources": [], "used_ai": False, "confidence": "none"}
    parts = [f"- {m['content']} (from {m['source']})" for m in matched]
    answer = "From your approved memories:\n" + "\n".join(parts)
    if styles:
        hints = "; ".join(s["content"] for s in styles)
        answer += f"\n(Noted how you like answers: {hints[:300]})"
    return {"answer": answer[:4000],
            "sources": [{"id": m["id"], "source": m["source"],
                         "category": m["category"]} for m in matched],
            "used_ai": False,
            "confidence": "high" if len(matched) > 1 else "low"}


def request_owner_input(conn, conv_id: int, actor_user_id: int,
                        question: str) -> int:
    """Record a twin's request for owner intervention (audit event)."""
    conv = engine.get_conversation(conn, conv_id)
    if conv is None:
        raise ValueError(f"Conversation {conv_id} not found.")
    text = (question or "").strip()
    if not text:
        raise ValueError("Intervention requests need a question.")
    return insert_returning_id(
        conn,
        "INSERT INTO audit_events (conv_id, action, actor_user_id, detail)"
        " VALUES (?, ?, ?, ?)",
        (conv_id, "owner.input_requested", actor_user_id, text[:500]))
