"""Deterministic mock twin brain (zero API calls).

Replies are assembled ONLY from the speaking twin's authorized public view
(plus approved memories in Phase 4). The mock never sees private fields, so
it cannot leak them. Behaviour is fully deterministic given the same state,
which keeps tests stable and demos reproducible.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .conversations import TwinContext

MAX_REPLY_CHARS = 600

_FOLLOW_UPS = (
    "What matters most to you here?",
    "Is there anything specific you'd like to know about my owner's work?",
    "What would a good outcome of this chat look like for you?",
    "Shall I share more about my owner's background?",
)

_RECRUIT_FOLLOW_UPS = (
    "What does success look like in this role?",
    "What is the timeline for your decision?",
    "Is there anything about my owner's background you'd like clarified?",
    "What are the next steps from your side?",
)


def _history_blob(ctx: "TwinContext") -> str:
    return "\n".join(body for _, body in ctx.history).lower()


def _fmt(value) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def _introduction(ctx: "TwinContext") -> str:
    view = ctx.public_view
    bits = [f"Hi {ctx.peer_name}! I'm {ctx.twin_name}, {ctx.owner_name}'s twin."]
    if view.get("headline"):
        bits.append(str(view["headline"]) + ".")
    elif view.get("company"):
        bits.append(f"I represent {view['company']}.")
    if ctx.opportunity:
        bits.append(f"I'd love to discuss '{ctx.opportunity['title']}'.")
    if view.get("skills"):
        bits.append(f"My owner's key skills: {_fmt(view['skills'])}.")
    need = _need_line(ctx)
    if need:
        bits.append(need)
    bits.append(_follow_up(ctx, 0))
    return _clip(" ".join(bits))


def _need_line(ctx: "TwinContext") -> str:
    """One line about the active request. Quotes route/time/skills only.

    The budget ceiling is NEVER quoted: it is the seeker's private
    reservation, enforced by matching code and the owner's decision.
    """
    summary = getattr(ctx, "request_summary", None) or {}
    origin, dest = summary.get("origin"), summary.get("destination")
    when = summary.get("when_text") or summary.get("when_iso")
    if origin and dest:
        line = f"I'm here about getting from {origin} to {dest}"
    elif dest:
        line = f"I'm here about getting to {dest}"
    elif summary.get("skills"):
        line = f"I'm here about {_fmt(summary['skills'])}"
    elif summary.get("category") and summary["category"] != "other":
        line = f"I'm here about a {summary['category']} request"
    else:
        return ""
    if when:
        line += f" ({when})"
    return line + "."


def _memory_hint(ctx: "TwinContext", question: str) -> str:
    qwords = {w.strip("?.,!").lower() for w in question.split() if len(w) > 3}
    best, best_overlap = None, 0
    for memory in ctx.memories:
        overlap = len(qwords & {w.lower() for w in memory.split()})
        if overlap > best_overlap:
            best, best_overlap = memory, overlap
    if best and best_overlap >= 2:
        return f" From my owner's notes: {best[:200]}"
    return ""


def _answer(ctx: "TwinContext", question: str) -> str:
    q = question.lower()
    view = ctx.public_view
    private_words = ("salary", "pay", "phone", "email", "address", "budget", "password")
    if any(w in q for w in private_words):
        return _clip(
            f"Good question, {ctx.peer_name}, but my owner hasn't authorized me "
            "to share that. I'm happy to pass along a request for them to answer directly."
        )
    if any(w in q for w in ("price", "cost", "rate", "charge", "fare", "fee",
                           "how much")) and "experience" not in q:
        quoted = None
        for key, value in view.items():
            if any(w in key.lower() for w in ("price", "rate", "fee", "fare", "cost")):
                if value not in (None, "", [], {}):
                    quoted = _fmt(value)
                    break
        if quoted:
            base = f"My owner's listed rate is {quoted}."
        else:
            base = ("My owner hasn't shared a rate I can quote. I can ask them "
                    "directly if you'd like.")
    elif any(w in q for w in ("skill", "do you know", "experience with", "stack", "technolog")):
        if view.get("skills"):
            base = f"My owner's key skills are {_fmt(view['skills'])}."
        elif view.get("requirements"):
            base = f"We're looking for {_fmt(view['requirements'])}."
        else:
            base = "My owner hasn't listed skills I can share yet."
    elif any(w in q for w in ("year", "experience", "long have", "senior")):
        if view.get("experience_years") is not None:
            base = f"My owner has {view['experience_years']} years of experience."
        else:
            base = "I don't have an authorized experience figure to share."
    elif any(w in q for w in ("where", "location", "based", "remote", "onsite", "on-site")):
        parts = []
        if view.get("location"):
            parts.append(f"My owner is based in {view['location']}.")
        if view.get("open_to_remote"):
            parts.append("Remote work is OK.")
        base = " ".join(parts) if parts else "I don't have a location I can share."
    elif any(w in q for w in ("availab", "when can", "start", "timeline")):
        base = (
            f"On availability: {_fmt(view['availability'])}."
            if view.get("availability")
            else "I'd need my owner to confirm availability."
        )
    elif any(w in q for w in ("portfolio", "work", "project", "show", "example")):
        if view.get("portfolio_url"):
            base = f"You can see my owner's work at {view['portfolio_url']}."
        else:
            base = "My owner hasn't shared a portfolio link I can pass on."
    elif any(w in q for w in ("compan", "hiring", "role", "position", "job")):
        if view.get("company"):
            base = (
                f"I represent {view['company']}"
                + (f", currently hiring for {view['role_hiring']}." if view.get("role_hiring") else ".")
            )
        else:
            base = "My owner is exploring new opportunities."
    elif any(w in q for w in ("who", "your owner", "about you", "introduce")):
        base = f"I'm {ctx.twin_name}, {ctx.owner_name}'s twin. {ctx.persona}"
    else:
        base = (
            "I've shared everything my owner authorized on that topic. "
            "I can ask them directly if you'd like to go deeper."
        )
    extra = _memory_hint(ctx, question)
    return _clip(f"{base}{extra} {_follow_up(ctx, len(ctx.history))}")


def _follow_up(ctx: "TwinContext", turn_index: int) -> str:
    pool = _RECRUIT_FOLLOW_UPS if ctx.purpose == "recruitment" else _FOLLOW_UPS
    return pool[turn_index % len(pool)]


def _share_and_ask(ctx: "TwinContext") -> str:
    blob = _history_blob(ctx)
    need = _need_line(ctx)
    if need and "i'm here about" not in blob:
        return _clip(f"{need} {_follow_up(ctx, len(ctx.history))}")
    skip = {"open_to_remote"}
    for key, value in ctx.public_view.items():
        if key in skip or value in (None, "", [], {}):
            continue
        text = _fmt(value)
        if text and text.lower() not in blob and len(text) < 220:
            label = key.replace("_", " ")
            return _clip(f"One more thing: {label} — {text}. {_follow_up(ctx, len(ctx.history))}")
    return _clip(f"Thanks, {ctx.peer_name}! {_follow_up(ctx, len(ctx.history))}")


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_REPLY_CHARS else text[: MAX_REPLY_CHARS - 1] + "…"


def generate(ctx: "TwinContext") -> str:
    peer_messages = [b for sender, b in ctx.history if sender == "peer"]
    if not peer_messages:
        return _introduction(ctx)
    last = peer_messages[-1]
    if "?" in last:
        return _answer(ctx, last)
    return _share_and_ask(ctx)
