"""Deterministic voice-command parsing (no AI, no network).

Transcripts arrive lowercase and messy from speech recognition, so
matching is case-insensitive and stop-word bounded. Anything the
parser cannot slot becomes a clarification question, never a guess.
"""

from __future__ import annotations

import re

KINDS = ("ask", "help", "status", "message", "project",
         "cancel", "unknown", "setup", "update", "undo", "chat",
         "teach", "remember", "approve_mem", "reject_mem", "find",
         "opportunities", "need", "my_chats")

_WAKE = re.compile(
    r"^(?:hey|hi|hello|ok|okay)[,\s]+(?:mira|twin)[,\s]+"
    r"|^(?:mira|twin)[,\s]+",
    re.IGNORECASE)

_CANCEL = re.compile(
    r"\b(cancel|stop|abort|never ?mind|forget it)\b", re.IGNORECASE)
_AFFIRM = re.compile(
    r"^(yes|yeah|yep|yup|sure|confirm|do it|send it|go ahead|ok|okay)"
    r"[\s.!]*$", re.IGNORECASE)
_DENY = re.compile(
    r"^(no|nope|nah|don't|cancel|stop)[\s.!]*", re.IGNORECASE)
_HELP = re.compile(
    r"\bhelp\b|what can you do|list commands", re.IGNORECASE)
_STATUS = re.compile(
    r"\bstatus\b|my projects?|how('| i)s it going|\busage\b|my spend",
    re.IGNORECASE)
_MESSAGE_VERB = re.compile(
    r"\b(send|message|text|dm|tell|write to|remind)\b", re.IGNORECASE)
_PROJECT_VERB = re.compile(
    r"\b(build|create|start|make|plan)\b", re.IGNORECASE)
_QUESTION = re.compile(
    r"^(who|what|where|when|why|how|do|does|did|is|are|was|were|"
    r"can|could|should|would|will|have|has|am|tell me|explain|"
    r"describe|remind me (?:what|who|when|where|why|how|about|of))"
    r"\b", re.IGNORECASE)

_CHANNEL_WORDS = {
    "telegram": "telegram",
    "whatsapp": "whatsapp",
    "messenger": "messenger",
    "instagram": "instagram",
    "insta": "instagram",
    "mock": "mock",
}
_SLOT_STOPS = ("on", "via", "over", "at", "that", "say", "saying",
               "telling", "tell", "and", "please")

_SETUP_VERB = re.compile(
    r"\b(create|set\s*up|setup|start|make|build)\b.{0,20}?\btwin\b",
    re.IGNORECASE)
_SETUP_GUARD = re.compile(
    r"\b(remember|teach|learn|update|talk|chat|speak|tell)\b",
    re.IGNORECASE)
_CHAT_ENTER = re.compile(
    r"\b(talk to|chat with|speak to|speak with|converse with)\s+"
    r"(my\s+)?twin\b", re.IGNORECASE)
_UPDATE_TRIGGER = re.compile(r"\bupdate\s+my\s+twin\b", re.IGNORECASE)
_UNDO = re.compile(r"^undo(\s+(that|it|last|change))?[\s.!]*$",
                   re.IGNORECASE)
_TEACH = re.compile(r"\bteach\b.{0,20}?\btwin\b|\bremember this\b"
                    r"|\bremember\s*:", re.IGNORECASE)
_REMEMBER = re.compile(
    r"\bwhat\s+do\s+you\s+remember\b|\bwhat\s+does\s+my\s+twin\s+"
    r"remember\b|\bshow\s+(my\s+)?(pending|memories)\b",
    re.IGNORECASE)
_APPROVE_MEM = re.compile(r"\bapprove\b.{0,20}?\bmemory\b"
                          r"|\bapprove\s+(latest|all\b.{0,10}?pending)",
                          re.IGNORECASE)
_REJECT_MEM = re.compile(r"\b(reject|delete|remove|forget)\b.{0,20}?"
                         r"\bmemory\b", re.IGNORECASE)
_OPPORTUNITIES = re.compile(r"\bopportunit|\bjob openings?\b"
                            r"|\bgigs?\b", re.IGNORECASE)
_FIND = re.compile(r"\bfind\b", re.IGNORECASE)
_NEED = re.compile(r"^\s*i\s+need\b", re.IGNORECASE)
_MY_CHATS = re.compile(r"\b(my\s+)?(chats|conversations)\b",
                       re.IGNORECASE)
_UPDATE_OP = re.compile(
    r"\b(add|remove|delete|drop|replace|change|swap|set|update|"
    r"clear|reset)\b", re.IGNORECASE)


def normalize(text: str) -> str:
    """Strip wake words and collapse whitespace (keeps original case)."""
    cleaned = " ".join((text or "").split())
    return _WAKE.sub("", cleaned).strip()


def is_affirm(text: str) -> bool:
    return bool(_AFFIRM.match((text or "").strip()))


def is_deny(text: str) -> bool:
    lowered = (text or "").strip().lower()
    return bool(_DENY.match(lowered)) or bool(
        _CANCEL.search(lowered))


def _slot_after_contact(words: list[str], start: int) -> str:
    taken = []
    for word in words[start:]:
        if word.lower() in _SLOT_STOPS or len(taken) >= 3:
            break
        taken.append(word)
    return " ".join(taken).strip(" ,.")


def _parse_message(cleaned: str) -> dict:
    words = cleaned.split()
    lowered = [w.lower() for w in words]
    slots: dict[str, str] = {}
    # Contact: words after "to", bounded by stop words.
    if "to" in lowered:
        slots["contact"] = _slot_after_contact(
            words, lowered.index("to") + 1)
    else:
        # "Text Ana ..." / "message Mom ...": name right after
        # the verb (articles skipped, two words max).
        verb_at = next(
            (i for i, w in enumerate(lowered)
             if _MESSAGE_VERB.fullmatch(w) or w == "write"),
            None)
        if verb_at is not None:
            rest = [w for w in words[verb_at + 1:]
                    if w.lower() not in ("a", "an", "the", "my",
                                         "message")]
            taken = []
            for word in rest:
                if (word.lower() in _SLOT_STOPS
                        or len(taken) >= 2):
                    break
                taken.append(word)
            if taken:
                slots["contact"] = " ".join(taken).strip(" ,.")
    # Channel: "<channel>" after on/via/over ("my" skipped).
    for prep in ("on", "via", "over"):
        if prep in lowered:
            nxt = [w for w in words[lowered.index(prep) + 1:]
                   if w.lower() != "my"]
            if nxt and nxt[0].lower().strip(" ,.") in _CHANNEL_WORDS:
                slots["channel"] = _CHANNEL_WORDS[
                    nxt[0].lower().strip(" ,.")]
            break
    # When: "at <time>" up to a clause boundary.
    when = re.search(
        r"\bat\s+([^,;]+?)(?=\s+and\s+|\s+that\s+|\s+tell\b|"
        r"\s+say\b|\s+saying\b|$)", cleaned, re.IGNORECASE)
    if when and when.group(1).strip():
        slots["when"] = when.group(1).strip()
    # Body: quoted text wins, else the clause after tell/say.
    quoted = re.search(r'"([^"]+)"', cleaned)
    if quoted:
        slots["body"] = quoted.group(1).strip()
    else:
        said = re.search(r"(?:tell|say|saying|telling)\s+(.+)$",
                         cleaned, re.IGNORECASE)
        if said and said.group(1).strip():
            rest = said.group(1).strip()
            first, gap, remainder = rest.partition(" ")
            contact_first = (slots.get("contact") or "").strip(
                ).lower().split()[:1]
            addressed = (
                first.lower() in ("him", "her", "them", "me", "us")
                or (bool(gap) and bool(remainder.strip())
                    and contact_first
                    and first.lower() == contact_first[0]))
            body = remainder.strip() if addressed else rest
            if body:
                slots["body"] = body.strip(" .")
    missing = [k for k in ("contact", "channel", "body")
               if not slots.get(k)]
    return {"kind": "message", "slots": slots, "missing": missing,
            "raw": cleaned}


def _parse_project(cleaned: str) -> dict:
    match = re.search(
        r"(?:build|create|start|make|plan)"
        r"(?:\s+(?:me|us|a|an|the|my|our|new))*"
        r"\s+(.+?)(?:\s+for\s+.+)?$",
        cleaned, re.IGNORECASE)
    title = (match.group(1).strip(" .") if match else "")
    slots = {"title": title, "goal": title} if title else {}
    missing = [] if title else ["title"]
    return {"kind": "project", "slots": slots, "missing": missing,
            "raw": cleaned}


def parse_command(text: str) -> dict:
    """Parse one transcript into a command (pure, deterministic)."""
    cleaned = normalize(text)
    if not cleaned:
        return {"kind": "unknown", "slots": {}, "missing": [],
                "raw": ""}
    if _CANCEL.search(cleaned) and len(cleaned.split()) <= 4:
        return {"kind": "cancel", "slots": {}, "missing": [],
                "raw": cleaned}
    if _SETUP_VERB.search(cleaned) and not _SETUP_GUARD.search(
            cleaned):
        return {"kind": "setup", "slots": {}, "missing": [],
                "raw": cleaned}
    if _CHAT_ENTER.search(cleaned):
        return {"kind": "chat", "slots": {}, "missing": [],
                "raw": cleaned}
    if _UPDATE_TRIGGER.search(cleaned):
        return {"kind": "update", "slots": {"text": cleaned},
                "missing": [], "raw": cleaned}
    if _UNDO.match(cleaned.strip()):
        return {"kind": "undo", "slots": {}, "missing": [],
                "raw": cleaned}
    if _TEACH.search(cleaned):
        return {"kind": "teach",
                "slots": {"fact": _teach_fact(cleaned)},
                "missing": [], "raw": cleaned}
    if _REMEMBER.search(cleaned):
        return {"kind": "remember",
                "slots": _remember_slots(cleaned),
                "missing": [], "raw": cleaned}
    if _APPROVE_MEM.search(cleaned):
        return {"kind": "approve_mem",
                "slots": _mem_number(cleaned),
                "missing": [], "raw": cleaned}
    if _REJECT_MEM.search(cleaned):
        return {"kind": "reject_mem",
                "slots": _mem_number(cleaned),
                "missing": [], "raw": cleaned}
    if _NEED.match(cleaned):
        return {"kind": "need", "slots": {"text": cleaned},
                "missing": [], "raw": cleaned}
    if _OPPORTUNITIES.search(cleaned):
        query = _opp_query(cleaned)
        return {"kind": "opportunities",
                "slots": {"query": query,
                          "skills": _split_skills(query)},
                "missing": [], "raw": cleaned}
    if _FIND.search(cleaned):
        query = _skill_query(cleaned)
        return {"kind": "find",
                "slots": {"query": query,
                          "skills": _split_skills(query)},
                "missing": [], "raw": cleaned}
    if _MY_CHATS.search(cleaned):
        return {"kind": "my_chats", "slots": {}, "missing": [],
                "raw": cleaned}
    if _HELP.search(cleaned):
        return {"kind": "help", "slots": {}, "missing": [],
                "raw": cleaned}
    if _STATUS.search(cleaned):
        return {"kind": "status", "slots": {}, "missing": [],
                "raw": cleaned}
    if cleaned.endswith("?") or _QUESTION.match(cleaned):
        return {"kind": "ask", "slots": {"question": cleaned},
                "missing": [], "raw": cleaned}
    if _MESSAGE_VERB.search(cleaned):
        return _parse_message(cleaned)
    if _PROJECT_VERB.search(cleaned):
        return _parse_project(cleaned)
    if _looks_like_update(cleaned):
        return {"kind": "update", "slots": {"text": cleaned},
                "missing": [], "raw": cleaned}
    return {"kind": "unknown", "slots": {}, "missing": [],
            "raw": cleaned}


def channel_word(text: str) -> str | None:
    """Map a spoken app name to a channel, or None."""
    return _CHANNEL_WORDS.get((text or "").strip().lower().strip(" ,."))


def _teach_fact(cleaned: str) -> str:
    match = re.search(
        r"teach\s+(?:my\s+twin\s+)?(?:that\s+|about\s+)?(.+)$",
        cleaned, re.IGNORECASE)
    if match:
        return match.group(1).strip(" .")
    match = re.search(r"remember\s*(?:this\s*)?:?\s*(.+)$",
                      cleaned, re.IGNORECASE)
    if match:
        fact = match.group(1).strip(" .")
        if fact.lower() not in ("", "this"):
            return fact
    return ""


def _remember_slots(cleaned: str) -> dict:
    topic = ""
    match = re.search(r"\babout\s+(.+)$", cleaned, re.IGNORECASE)
    if match:
        topic = match.group(1).strip(" .?")
    return {"topic": topic,
            "pending_only": "pending" in cleaned.lower()}


def _mem_number(cleaned: str) -> dict:
    match = re.search(r"(?:memory\s+|#|number\s+)(\d+)", cleaned,
                      re.IGNORECASE)
    if match:
        return {"n": int(match.group(1))}
    if "latest" in cleaned.lower():
        return {"n": "latest"}
    return {"n": None}


def _skill_query(cleaned: str) -> str:
    text = re.sub(r"^find\s+me\s+(a\s+|an\s+|some\s+)?", "",
                  cleaned, flags=re.IGNORECASE).strip()
    text = re.sub(
        r"^(find\s+)?(someone|somebody|a\s+person|a\s+twin|help)\s+"
        r"(who\s+can\s+|that\s+can\s+|to\s+|for\s+)?", "",
        text, flags=re.IGNORECASE).strip()
    text = re.sub(r"^(help\s+me\s+with|help\s+with|for|with|about|in)"
                  r"\s+", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"^(developers?|designers?|people|person)\s+"
                  r"(for|with|in)\s+", "", text,
                  flags=re.IGNORECASE).strip()
    return text.strip(" .?")


def _opp_query(cleaned: str) -> str:
    match = re.search(r"(?:for|with|about|in)\s+(.+)$", cleaned,
                      re.IGNORECASE)
    if match:
        return match.group(1).strip(" .?")
    return ""


def _split_skills(query: str) -> list[str]:
    parts = re.split(r",|\band\b", query or "")
    return [p.strip(" .") for p in parts if p.strip(" .")][:5]


def _looks_like_update(cleaned: str) -> bool:
    if not _UPDATE_OP.search(cleaned):
        return False
    from ..twin_update_nl import FIELDS

    lowered = f" {cleaned.lower().strip(' .!?')} "
    for _field, (_kind, aliases) in FIELDS.items():
        for alias in aliases:
            if f" {alias} " in lowered:
                return True
    return False
