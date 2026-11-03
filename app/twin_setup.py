"""Draft-only typed questionnaire for the existing twin creation form.

Skipped questions stay empty. This helper never persists a twin; the
owner reviews and submits the normal form after the draft.

State shape carried between setup form posts::
    {"answers": {field: value}, "step": int, "phase": "collect"|"review"}
"""

from __future__ import annotations

import re

#: (field, question). Order is the interview order.
STEPS: tuple[tuple[str, str], ...] = (
    ("display_name", "What should I call your twin?"),
    ("headline", "What one-line headline describes you? You can say skip."),
    ("role", "Which best describes your twin: job_seeker, employer, collaboration, professional, independent, or other?"),
    ("skills", "What are your personal skills? List them with commas; skip if none."),
    ("experience_years", "How many years of experience do you have? Enter a number or skip."),
    ("location", "What city or area are you based in?"),
    ("availability", "When are you generally available?"),
    ("interests", "What are your interests? List them with commas or skip."),
    ("bio", "Anything else your twin should know about you?"),
    ("tone", "How should your twin communicate? For example, friendly and concise."),
)

#: Words that address a collected field for change/edit commands.
FIELD_ALIASES = {
    "display_name": ("name", "display name", "twin name", "called"),
    "headline": ("headline", "title", "tagline"),
    "skills": ("skill", "skills"),
    "interests": ("interest", "interests", "hobbies", "hobby"),
    "tone": ("style", "tone", "communication", "communicate"),
    "experience_years": ("experience years", "years of experience"),
    "role": ("role", "twin role"),
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
    """Build only fields supported by the current form; skills stay personal."""
    years = None
    raw_years = answers.get("experience_years")
    if raw_years:
        try:
            years = max(0, min(80, int(raw_years)))
        except (TypeError, ValueError):
            years = None
    return {
        "headline": answers.get("headline") or "",
        "role": answers.get("role") or "",
        "skills": list(answers.get("skills") or []),
        "experience_years": years,
        "interests": list(answers.get("interests") or []),
        "bio": answers.get("bio") or "",
        "location": answers.get("location") or "",
        "availability": answers.get("availability") or "",
    }


def review_text(state: dict) -> str:
    return ("Here is your draft. Nothing has been saved. Continue to the"
            " existing profile form to review sharing settings and create"
            " your private twin:\n" + _summary(state["answers"]))


def setup_turn(state: dict, text: str) -> dict:
    """Advance a draft interview; persistence is handled by the twin form."""
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

    if phase == "paused":
        if heard("resume", "resume setup", "continue setup"):
            state["phase"] = "collect"
            return {"reply": current_question(state), "done": False,
                    "cancelled": False}
        return {"reply": "Your draft is still on this page. Say 'resume' to continue.",
                "done": False, "cancelled": False}

    if phase == "review":
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
        return {"reply": "Continue to the profile form to review and submit"
                " your twin, or say 'edit <field>' to change an answer."
                " Say 'summary' to see the draft again.",
                "done": False, "cancelled": False}

    # collect phase commands
    if heard("cancel", "stop", "abort", "never mind", "nevermind",
             "forget it", "quit", "exit"):
        return {"reply": "Twin creation cancelled. Nothing was saved.",
                "done": True, "approved": False, "cancelled": True}
    if heard("pause", "pause this", "hold on", "wait"):
        state["phase"] = "paused"
        return {"reply": "Paused. Your draft remains on this page. Say 'resume' to continue.",
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
    elif target in ("skills", "interests"):
        state["answers"][target] = _csv(cleaned)
    elif target == "display_name":
        state["answers"][target] = _clip(cleaned, 60)
    elif target in ("tone", "role", "experience_years"):
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

