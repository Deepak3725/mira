"""Conversational twin creation (Part 1).

One question at a time, shared by voice and typed text: both call
``setup_turn`` against the same per-user state, so switching
interfaces never loses progress. Nothing is invented: skipped
questions stay empty, and the twin is created (private) only after
the owner approves the summary.

State shape (kept by the caller, e.g. the voice session)::
    {"answers": {field: value}, "step": int, "phase": "collect"|"review"}
"""

from __future__ import annotations

import re

from .conversations import audit
from .db import create_twin, get_twin_by_user

#: (field, question). Order is the interview order.
STEPS: tuple[tuple[str, str], ...] = (
    ("display_name",
     "What should I call your twin?"),
    ("headline",
     "Give your twin a one-line headline, like 'Fullstack developer"
     " for hire'. (Say 'skip' for any question.)"),
    ("education",
     "What is your education background?"),
    ("experience",
     "Summarize your professional experience in a sentence or two."),
    ("skills",
     "List your skills, separated by commas."),
    ("interests",
     "What are your interests?"),
    ("preferences",
     "Any work or communication preferences I should know?"),
    ("comm_style",
     "How should your twin communicate? For example: friendly and"
     " brief, formal, or technical."),
    ("goals",
     "What are your professional goals?"),
    ("bio",
     "Anything else your twin should know about you?"),
)

#: Words that address a collected field for change/edit commands.
FIELD_ALIASES = {
    "display_name": ("name", "display name", "twin name", "called"),
    "headline": ("headline", "title", "tagline"),
    "education": ("education", "school", "degree", "university",
                  "college", "studied"),
    "experience": ("experience", "work", "job", "career", "history"),
    "skills": ("skill", "skills"),
    "interests": ("interest", "interests", "hobbies", "hobby"),
    "preferences": ("preference", "preferences", "likes"),
    "comm_style": ("style", "tone", "communication", "voice",
                   "communicate"),
    "goals": ("goal", "goals", "ambition", "aim"),
    "bio": ("bio", "about", "other", "background", "rest"),
}

_YEARS = re.compile(r"(\d{1,2})\s*(?:years?|yrs?)", re.IGNORECASE)


def _clip(text: str, limit: int) -> str:
    return " ".join((text or "").split())[:limit]


def _csv(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for chunk in (text or "").split(","):
        item = " ".join(chunk.split())
        if item and item.lower() not in seen:
            seen.add(item.lower())
            out.append(item[:120])
    return out[:30]


def new_setup() -> dict:
    return {"answers": {}, "step": 0, "phase": "collect"}


def current_field(state: dict) -> str | None:
    if state.get("phase") != "collect":
        return None
    idx = state.get("step", 0)
    if idx >= len(STEPS):
        return None
    return STEPS[idx][0]


def current_question(state: dict) -> str:
    field = current_field(state)
    if field is None:
        return ""
    return STEPS[state.get("step", 0)][1]


def field_from_words(text: str) -> str | None:
    lowered = f" {(text or '').lower()} "
    for field, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            if f" {alias} " in lowered or lowered.strip() == alias:
                return field
    return None


def _is_skip(text: str) -> bool:
    return (text or "").strip().lower() in (
        "skip", "skip it", "skip this", "pass", "next", "none",
        "nothing", "n/a")


def _summary(answers: dict) -> str:
    lines = []
    for field, _question in STEPS:
        value = answers.get(field)
        if isinstance(value, list):
            shown = ", ".join(value) if value else "(skipped)"
        else:
            shown = value if value else "(skipped)"
        lines.append(f"- {field}: {shown}")
    return "\n".join(lines)


def profile_from_answers(answers: dict) -> dict:
    """Build the twin profile dict. Only stated facts, never guesses."""
    skills = answers.get("skills")
    interests = answers.get("interests")
    preferences = answers.get("preferences")
    experience = answers.get("experience") or ""
    years = None
    found = _YEARS.search(experience)
    if found:
        years = max(0, min(80, int(found.group(1))))
    return {
        "headline": answers.get("headline") or "",
        "skills": list(skills) if skills else [],
        "experience_years": years,
        "experience": experience,
        "education": answers.get("education") or "",
        "interests": list(interests) if interests else [],
        "preferences": list(preferences) if preferences else [],
        "goals": answers.get("goals") or "",
        "bio": answers.get("bio") or "",
        "location": "",
        "availability": "",
    }


def review_text(state: dict) -> str:
    return ("Here is your twin's draft. It stays private until you say"
            " so. Say 'approve' to activate it, or 'edit <field>' to"
            " change something:\n" + _summary(state["answers"]))


def setup_turn(state: dict, text: str) -> dict:
    """Advance the interview. Returns {"reply", "done", "cancelled"}.

    ``done`` with ``approved`` True means the caller should persist
    the twin via ``activate``.
    """
    cleaned = _clip(text, 2000)
    lowered = re.sub(r"[.!?]+$", "", cleaned.lower()).strip()
    phase = state.get("phase", "collect")

    def heard(*phrases: str) -> bool:
        if lowered in phrases:
            return True
        # Short trigger phrases ("cancel setup") count; longer
        # sentences that merely start with one are answers.
        if len(lowered.split()) > 3:
            return False
        return any(lowered.startswith(p + " ") for p in phrases)

    if phase == "review":
        if heard("approve", "approved", "yes", "yeah", "yep",
                 "activate", "looks good", "confirm", "do it",
                 "go ahead"):
            return {"reply": "", "done": True, "approved": True,
                    "cancelled": False}
        if heard("cancel", "stop", "abort", "never mind",
                 "nevermind", "forget it", "quit", "exit"):
            return {"reply": "Twin creation cancelled. Nothing was"
                    " saved.", "done": True, "approved": False,
                    "cancelled": True}
        if heard("summary", "show summary", "show me", "review"):
            return {"reply": review_text(state), "done": False,
                    "cancelled": False}
        field = field_from_words(cleaned)
        if field is not None and any(
                w in lowered for w in ("edit", "change", "correct",
                                       "update", "fix")):
            state["phase"] = "collect"
            state["step"] = next(i for i, (f, _q) in enumerate(STEPS)
                                 if f == field)
            state["resume_review"] = True
            return {"reply": f"OK, {_summary_question(field)}",
                    "done": False, "cancelled": False}
        return {"reply": "Say 'approve' to activate your twin, or"
                " 'edit <field>' to change an answer. Say 'summary'"
                " to see the draft again.",
                "done": False, "cancelled": False}

    # collect phase commands
    if heard("cancel", "stop", "abort", "never mind", "nevermind",
             "forget it", "quit", "exit"):
        return {"reply": "Twin creation cancelled. Nothing was saved.",
                "done": True, "approved": False, "cancelled": True}
    if heard("pause", "pause this", "hold on", "wait"):
        return {"reply": "Paused. Say 'resume' any time to continue"
                " where you left off.",
                "done": False, "cancelled": False, "paused": True}
    if heard("summary", "show summary", "show me", "review",
             "what do you have", "what have you got"):
        return {"reply": _summary(state["answers"])
                + f"\n{current_question(state)}",
                "done": False, "cancelled": False}
    if heard("back", "go back", "previous", "previous question"):
        state["step"] = max(0, state.get("step", 0) - 1)
        return {"reply": current_question(state), "done": False,
                "cancelled": False}
    field = field_from_words(cleaned)
    if field is not None and any(
            w in lowered for w in ("change", "correct", "edit",
                                   "update", "fix", "redo")):
        state["step"] = next(i for i, (f, _q) in enumerate(STEPS)
                             if f == field)
        return {"reply": f"OK, {_summary_question(field)}",
                "done": False, "cancelled": False}

    # an answer to the current question
    target = current_field(state)
    if target is None:
        state["phase"] = "review"
        return {"reply": review_text(state), "done": False,
                "cancelled": False}
    if _is_skip(cleaned):
        state["answers"].pop(target, None)
    elif target in ("skills", "interests", "preferences"):
        state["answers"][target] = _csv(cleaned)
    elif target == "display_name":
        state["answers"][target] = _clip(cleaned, 60)
    elif target == "comm_style":
        state["answers"][target] = _clip(cleaned, 120)
    else:
        state["answers"][target] = _clip(cleaned, 1000)
    state["step"] = state.get("step", 0) + 1
    if state.pop("resume_review", False):
        state["phase"] = "review"
        return {"reply": review_text(state), "done": False,
                "cancelled": False}
    if state["step"] >= len(STEPS):
        state["phase"] = "review"
        return {"reply": review_text(state), "done": False,
                "cancelled": False}
    return {"reply": current_question(state), "done": False,
            "cancelled": False}


def _summary_question(field: str) -> str:
    for name, question in STEPS:
        if name == field:
            return question
    return "What should it be?"


def start_text(owner_name: str) -> str:
    name = f" {owner_name}" if owner_name else ""
    return (f"Let's create your twin{name}. I'll ask a few questions"
            f" one at a time; say 'skip' for any of them. "
            f"{STEPS[0][1]}")


def activate(conn, user_id: int, owner_name: str,
             state: dict) -> dict:
    """Persist the approved draft as a PRIVATE twin. Returns a report."""
    if get_twin_by_user(conn, user_id) is not None:
        raise ValueError("You already have a twin. Say 'update my"
                         " twin' to change it.")
    answers = state.get("answers", {})
    display = answers.get("display_name") or (
        f"{owner_name}'s twin" if owner_name else "My twin")
    tone = answers.get("comm_style") or "friendly"
    twin_id = create_twin(
        conn, user_id=user_id, display_name=_clip(display, 60)[:60],
        persona="A helpful personal twin.",
        tone=tone, profile=profile_from_answers(answers),
        authorized_fields=[], avatar_seed=f"user-{user_id}-twin",
        is_public=False)
    audit(conn, None, "twin.setup_completed", user_id,
          f"twin={twin_id} fields={len(answers)}")
    conn.commit()
    filled = sum(1 for _f, _q in STEPS if answers.get(_f))
    return {"twin_id": twin_id, "display_name": display,
            "filled": filled, "total": len(STEPS)}
