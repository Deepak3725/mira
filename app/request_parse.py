"""Deterministic natural-language need parser (zero AI calls).

Mock implementation of the future "understand the request" AI slot: extracts
structured requirements from one need statement with fixed rules. Anything it
cannot parse goes into ``unparsed`` so the owner can correct it -- it never
invents values.

Extracts (all optional):
  budget_max      -- "Nu. 500", "max 500", "under $50", "budget of 300"
  origin          -- "from GCIT"
  destination     -- "to Thimphu"
  when            -- "tomorrow at 9 AM" (kept as raw text + best-effort ISO date)
  skills          -- "needs Python, SQL" / "with X experience"
  category_hint   -- taxi/ride, job/hire, service, supplier, collaborator
"""

from __future__ import annotations

import datetime as _dt
import re

CATEGORIES = ("transport", "job", "service", "supply", "collaboration", "other")

_CATEGORY_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("transport", ("taxi", "ride", "driver", "cab", "pickup", "drop", "airport", "flight")),
    ("job", ("job", "hire", "hiring", "employee", "candidate", "recruit", "developer", "designer")),
    ("service", ("plumber", "electrician", "repair", "clean", "cater", "photograph", "tutor")),
    ("supply", ("supplier", "supply", "wholesale", "units of", "order of", "vendor")),
    ("collaboration", ("collaborat", "partner", "cofounder", "co-founder", "team up")),
)

_BUDGET_RE = re.compile(
    r"(?:nu\.?|btn|rs\.?|\$)?\s*(\d[\d,]*)\s*(?:nu\.?|btn|rs\.?|ngultrum)?"
    r"|(?:max(?:imum)?|budget|under|up to|at most|no more than)\s*"
    r"(?:nu\.?|btn|rs\.?|\$)?\s*(\d[\d,]*)",
    re.IGNORECASE,
)
_BUDGET_NEAR = re.compile(r"(max|budget|under|up to|at most|no more than|nu\.?|btn|rs\.?|\$)", re.IGNORECASE)

_PLACE_RE = re.compile(
    r"\bfrom\s+([A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,2})"
    r"|\bto\s+([A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,2})",
)

_WHEN_WORDS = ("today", "tomorrow", "monday", "tuesday", "wednesday", "thursday",
               "friday", "saturday", "sunday", "am", "pm", "morning", "afternoon",
               "evening", "weekend", "next week")

_SKILL_RE = re.compile(
    r"(?:needs?|requires?|requiring|with|skilled in|experience in|knows?|knowing)\s+"
    r"([A-Za-z][\w+#.]*(?:\s*(?:,|and|&)\s*[A-Za-z][\w+#.]*)*)",
    re.IGNORECASE,
)
_SKILL_STOP = {"a", "an", "the", "my", "me", "good", "great", "some", "experience",
               "years", "year", "with", "and", "in", "of", "for", "to",
               "help", "helping", "someone", "something", "anything", "please"}


def _parse_budget(text: str) -> tuple[float | None, str | None]:
    for match in _BUDGET_RE.finditer(text):
        raw = match.group(1) or match.group(2)
        start = max(0, match.start() - 24)
        context = text[start:match.end() + 8]
        if not _BUDGET_NEAR.search(context):
            continue
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        currency = "Nu."
        low = context.lower()
        if "$" in context:
            currency = "$"
        elif "rs" in low:
            currency = "Rs."
        return value, currency
    return None, None


def _parse_places(text: str) -> tuple[str | None, str | None, str]:
    origin = destination = None
    for match in _PLACE_RE.finditer(text):
        if match.group(1) and origin is None:
            origin = match.group(1).strip(" .")
        if match.group(2) and destination is None:
            destination = match.group(2).strip(" .")
    return origin, destination, text


def _parse_when(text: str) -> tuple[str | None, str | None]:
    low = text.lower()
    hits = [w for w in _WHEN_WORDS if re.search(r"\b" + w + r"\b", low)]
    if not hits:
        return None, None
    timed = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", low)
    time_part = ""
    if timed:
        hour = int(timed.group(1)) % 12 + (12 if timed.group(3) == "pm" else 0)
        time_part = f" {hour:02d}:{timed.group(2) or '00'}"
    iso = None
    today = _dt.date.today()
    if "tomorrow" in hits:
        iso = (today + _dt.timedelta(days=1)).isoformat()
    elif "today" in hits:
        iso = today.isoformat()
    raw_bits = [w for w in ("tomorrow" if "tomorrow" in hits else "today" if "today" in hits else hits[0],)]
    return (raw_bits[0] + (f" at{time_part}" if time_part else "")), (iso + time_part.replace(" ", "T") if iso else None)


def _parse_skills(text: str) -> list[str]:
    found: list[str] = []
    for match in _SKILL_RE.finditer(text):
        for chunk in re.split(r",|\band\b|&", match.group(1)):
            word = chunk.strip().strip(".")
            if not word or word.lower() in _SKILL_STOP or len(word) > 40:
                continue
            if word not in found:
                found.append(word)
    return found[:10]


def _parse_category(text: str) -> str:
    low = text.lower()
    for category, words in _CATEGORY_WORDS:
        if any(w in low for w in words):
            return category
    return "other"


def parse_need(text: str) -> dict:
    """Parse one need statement. Pure function, fully deterministic."""
    text = " ".join((text or "").split())
    if not text:
        return {"requirements": {}, "category": "other", "unparsed": "",
                "warnings": ["Empty request."]}
    budget, currency = _parse_budget(text)
    origin, destination, _ = _parse_places(text)
    when_raw, when_iso = _parse_when(text)
    skills = _parse_skills(text)
    category = _parse_category(text)
    requirements: dict = {}
    if budget is not None:
        requirements["budget_max"] = budget
        requirements["budget_currency"] = currency
    if origin:
        requirements["origin"] = origin
    if destination:
        requirements["destination"] = destination
    if when_raw:
        requirements["when_text"] = when_raw
    if when_iso:
        requirements["when_iso"] = when_iso
    if skills:
        requirements["skills"] = skills
    warnings = []
    if category == "other":
        warnings.append("Category unclear; please confirm it below.")
    if not requirements:
        warnings.append("No structured details found; please fill them in below.")
    return {"requirements": requirements, "category": category,
            "unparsed": text, "warnings": warnings}
