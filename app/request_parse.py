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

# NOTE (hackathon compliance): no "job" hiring category. Employment
# screening is prohibited, so hiring-speak parses as "other" and job
# discovery happens seeker-side via opportunities, never as applicant
# matching. Service/transport/supply/collaboration stay: coordination,
# not employment screening.
CATEGORIES = ("transport", "service", "supply", "collaboration", "other")

_CATEGORY_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("transport", ("taxi", "ride", "driver", "cab", "pickup", "drop", "airport", "flight")),
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
    # "From"/"To" match case-insensitively (sentence starts), but
    # place names stay capitalized so "from here" never parses.
    # "for X to Y" (offer phrasing) yields origin X as well.
    r"(?i:\bfrom)\s+([A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,2})"
    r"|(?i:\bto)\s+([A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,2})"
    r"|(?i:\bfor)\s+([A-Z][\w.'-]*)\s+(?=to\b)",
)

_WHEN_WORDS = ("today", "tomorrow", "monday", "tuesday", "wednesday", "thursday",
               "friday", "saturday", "sunday", "am", "pm", "morning", "afternoon",
               "evening", "weekend", "next week")

_SKILL_RE = re.compile(
    # Triggers are standalone words: "learning" inside the compound
    # "machine-learning" must not trigger ("Find a machine-learning
    # tutor" finds no deterministic skill so the model can fill it).
    r"(?<![\w-])(?:needs?|requires?|requiring|with|skilled in|experience in|knows?|knowing"
    r"|learn(?:ing)?\b|looking for)\s+"
    r"([A-Za-z][\w+#.]*(?:\s*(?:,|and|&)\s*[A-Za-z][\w+#.]*)*)",
    re.IGNORECASE,
)
_SKILL_STOP = {"a", "an", "the", "my", "me", "good", "great", "some", "experience",
               "years", "year", "with", "and", "in", "of", "for", "to",
               "help", "helping", "someone", "something", "anything", "please"}

_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                 "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
                 "a": 1, "an": 1}

_UNIT_MINUTES = {"minute": 1, "minutes": 1, "min": 1, "mins": 1,
                 "hour": 60, "hours": 60, "hr": 60, "hrs": 60,
                 "day": 1440, "days": 1440,
                 "week": 10080, "weeks": 10080}

_DEADLINE_RE = re.compile(
    r"(?:deadline|within|for)\s+(?:of\s+|the\s+|a\s+)?"
    r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"(minutes?|mins?|hours?|hrs?|days?|weeks?)",
    re.IGNORECASE,
)

_QUANTITY_RE = re.compile(
    r"\b(?:find|need|needs|want|wants|looking\s+for)\b[^.?]{0,40}?\b"
    r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"([a-zA-Z][\w.-]*)",
    re.IGNORECASE,
)
_QUANTITY_STOP = {"am", "pm", "o'clock", "nu", "btn", "rs", "dollar",
                  "dollars", "buck", "bucks", "hour", "hours", "day",
                  "days", "week", "weeks", "minute", "minutes"}


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
        if match.group(3) and origin is None:
            origin = match.group(3).strip(" .")
    return origin, destination, text


def parse_places(text: str) -> tuple[str | None, str | None]:
    """Best-effort (origin, destination); single shared implementation."""
    origin, destination, _ = _parse_places(text or "")
    return origin, destination


def _parse_when(text: str) -> tuple[str | None, str | None]:
    low = text.lower()
    timed = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", low)
    hits = [w for w in _WHEN_WORDS if re.search(r"\b" + w + r"\b", low)]
    if not timed:
        # Bare "am"/"pm" without a clock time is the verb "am", not a
        # time ("I am looking ..." must not store when_text="am").
        hits = [w for w in hits if w not in ("am", "pm")]
    if not hits:
        return None, None
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
    if "tomorrow" in hits:
        raw_day = "tomorrow"
    elif "today" in hits:
        raw_day = "today"
    else:
        others = [w for w in hits if w not in ("am", "pm")]
        raw_day = others[0] if others else ""
    # Time-only keeps "at 09:00", never "am at 09:00".
    raw = (raw_day + (f" at{time_part}" if time_part else "")).strip()
    return raw or None, (iso + time_part.replace(" ", "T") if iso else None)


def _parse_skills(text: str) -> list[str]:
    found: list[str] = []
    for match in _SKILL_RE.finditer(text):
        for chunk in re.split(r",|\band\b|&", match.group(1)):
            word = chunk.strip().strip(".")
            if not word or word.lower() in _SKILL_STOP or len(word) > 40:
                continue
            first = word.split()[0].strip(",.").lower()
            if first in _NUMBER_WORDS or first[:1].isdigit():
                # Quantities/specs are counts, not skills ("four
                # seats", "16GB RAM", "2 years experience").
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


def _parse_number(raw: str) -> int | None:
    raw = (raw or "").strip().lower()
    if raw.isdigit():
        return int(raw)
    return _NUMBER_WORDS.get(raw)


def _parse_deadline(text: str) -> int | None:
    """Search duration in minutes ("deadline two hours", "within one
    day"). None when the owner states no duration."""
    match = _DEADLINE_RE.search(text or "")
    if not match:
        return None
    number = _parse_number(match.group(1))
    unit = _UNIT_MINUTES.get(match.group(2).lower())
    if not number or not unit:
        return None
    return number * unit


def _parse_quantity(text: str) -> int | None:
    """How-many count ("find me four passengers"). None when unstated.

    Times, currencies, and durations never count as quantities.
    """
    for match in _QUANTITY_RE.finditer(text or ""):
        if match.group(2).lower() in _QUANTITY_STOP:
            continue
        number = _parse_number(match.group(1))
        if number and number > 1:
            return number
    return None


def parse_need(text: str) -> dict:
    """Parse one need statement. Pure function, fully deterministic."""
    text = " ".join((text or "").split())
    if not text:
        return {"requirements": {}, "category": "other", "unparsed": "",
                "warnings": ["Empty request."], "deadline_minutes": None,
                "quantity": None}
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
            "unparsed": text, "warnings": warnings,
            "deadline_minutes": _parse_deadline(text),
            "quantity": _parse_quantity(text)}
