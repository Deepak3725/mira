"""Spec builder + clarifier (deterministic; AI-optional).

MIRA understands an instruction, asks clarification questions when it
is vague, then produces a project specification: requirements plus a
task list with dependencies. The deterministic path needs no model;
`draft_with_ai` upgrades the draft through the Phase 1 orchestrator
when a real provider is configured (mock-safe: mock returns the
draft unchanged).
"""

from __future__ import annotations

import json
import re


def clarify_questions(goal: str) -> list[str]:
    """Questions to ask before speccing a vague goal (may be empty)."""
    text = (goal or "").strip()
    if not text:
        return ["What should MIRA build? Describe the goal in a sentence."]
    questions = []
    if len(text.split()) < 6:
        questions.append(
            f"Your goal is {text!r}. What should the finished result do?")
    lowered = text.lower()
    if not re.search(r"\b(web|site|website|app|mobile|desktop|api|bot|"
                     r"report|document|script|dashboard)\b", lowered):
        questions.append("What kind of deliverable is this "
                         "(website, app, report, script, ...)?")
    if not re.search(r"\bfor\b", lowered):
        questions.append("Who is it for?")
    return questions[:4]


def draft_spec(goal: str, answers: dict | None = None) -> dict:
    """Build a first spec: requirements + task DAG outline."""
    text = (goal or "").strip()
    answers = answers or {}
    deliverable = answers.get("deliverable") or _guess_deliverable(text)
    audience = answers.get("audience") or "the owner"
    tasks = [
        {"key": "research", "kind": "research",
         "title": "Research requirements",
         "instructions": f"List what {text!r} must do for {audience}.",
         "deps": [], "needs_approval": False},
        {"key": "design", "kind": "writing",
         "title": "Write the design",
         "instructions": f"Write a short design for the {deliverable}.",
         "deps": ["research"], "needs_approval": True},
        {"key": "build", "kind": "code",
         "title": "Build the deliverable",
         "instructions": f"Implement the {deliverable} per the design.",
         "deps": ["design"], "needs_approval": False},
        {"key": "test", "kind": "test",
         "title": "Test the deliverable",
         "instructions": "Write and run checks for the deliverable; "
                          "report pass/fail per check.",
         "deps": ["build"], "needs_approval": False},
        {"key": "review", "kind": "review",
         "title": "Review and integrate",
         "instructions": "Review the work against the requirements, "
                          "fix gaps, and integrate the final result.",
         "deps": ["test"], "needs_approval": True},
    ]
    return {"goal": text, "deliverable": deliverable,
            "audience": audience,
            "requirements": [f"{deliverable} serves {audience}.",
                             "Each task below is done and reviewed."],
            "tasks": tasks}


def _guess_deliverable(text: str) -> str:
    lowered = text.lower()
    for word in ("website", "app", "report", "script", "dashboard",
                 "bot", "api"):
        if word in lowered:
            return word
    return "deliverable"


def draft_with_ai(conn, goal: str, answers: dict | None = None,
                  user_id=None, provider_name: str | None = None) -> dict:
    """Upgrade the deterministic draft via the AI orchestrator.

    Falls back to the deterministic draft whenever the provider is
    mock, unconfigured, or returns nothing usable.
    """
    from ..ai import new_project, new_task, run_task

    draft = draft_spec(goal, answers)
    try:
        project = new_project("spec-draft", goal)
        task = new_task(project, "spec", "writing",
                        "Improve this project spec as JSON.")
        result = run_task(
            conn, task,
            messages=[{"role": "user", "content":
                       "Improve this spec; reply with the same JSON "
                       f"shape, refined: {draft!r}"[:3000]}],
            user_id=user_id, provider_name=provider_name)
    except Exception:  # noqa: BLE001 - AI is optional here
        return draft
    if result.mock:
        return draft
    try:
        refined = json.loads(result.text)
    except ValueError:
        return draft
    if (not isinstance(refined, dict)
            or not isinstance(refined.get("tasks"), list)
            or not refined["tasks"]
            or not all(isinstance(t, dict) and t.get("key")
                       and t.get("title") for t in refined["tasks"])):
        return draft
    refined.setdefault("goal", draft["goal"])
    refined.setdefault("deliverable", draft["deliverable"])
    refined.setdefault("audience", draft["audience"])
    refined.setdefault("requirements", draft["requirements"])
    return refined
