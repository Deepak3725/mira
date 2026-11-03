"""Post Something service: one natural-language description becomes
a need or an offer through the existing engines.

No recipients, no email fields: the authenticated user is the owner.
Details are inferred where possible; the review step shows only what
was understood plus blank fields for genuine gaps.
"""

from __future__ import annotations

import re
import uuid
from sqlite3 import Connection

from . import matcher
from . import requests as need_engine
from .db import create_opportunity
from .request_parse import parse_need

#: Strong offer signals: providing/hiring wins over "need" phrasing
#: ("I'm hiring ... I need someone with X" is still an offer).
#: Employment seeking ("looking for an employee", "need a frontend
#: developer") is also an offer: the poster provides the job, and
#: seekers match it with their needs.
#: Employment roles that mark the poster as the job provider. Kept
#: narrow on purpose: "need a designer/plumber" is usually a service
#: need, matched by providers' offers — only unambiguous hiring
#: phrasing posts as an offer (the review step can override either way).
_EMPLOYMENT_ROLES = (
    r"employee|staff|intern|hire|co-?founder|developer|engineer"
)
_STRONG_OFFER = (
    r"\bhiring\b",
    r"\bwe\s+are\s+hiring\b",
    r"\bjoin\s+(my|our|the)\s+team\b",
    r"\bi\s+offer\b",
    r"\bwe\s+offer\b",
    r"\boffering\b",
    r"\bcan\s+provide\b",
    r"\bfor\s+rent\b",
    r"\bfor\s+hire\b",
    r"\bi\s+(teach|give\s+\w+\s+lessons)\b",
    r"\blessons\b.{0,20}\b(on\s+weekends|available)\b",
    r"\bi\s+have\b.{0,30}\b(taxi|car|room|seat|space)\b",
    # One modifier word may sit between the article and the role
    # ("looking for a frontend developer", "need a senior engineer").
    r"\blooking\s+for\s+(?:an?\s+)?(?:[A-Za-z][\w+#.-]*\s+)?("
    + _EMPLOYMENT_ROLES + r")\b",
    r"\bneed\s+(?:an?\s+)?(?:[A-Za-z][\w+#.-]*\s+)?("
    + _EMPLOYMENT_ROLES + r")\b",
    r"\bseeking\s+(?:an?\s+)?(?:[A-Za-z][\w+#.-]*\s+)?(employee|staff"
    r"|intern|hire|co-?founder)\b",
)
_STRONG_OFFER_RE = re.compile("|".join(_STRONG_OFFER), re.IGNORECASE)

#: Explicit need phrasing beats weak "available" ("I need someone
#: available on weekends" is a need, not an offer).
#: "I need X for rent" needs the rental (a need); "I have X for
#: rent" provides it (an offer). Questions asking if anyone is
#: available are needs too.
_NEED_RENTAL_RE = re.compile(
    r"\b(need|want)\b.{0,20}\bfor\s+(rent|hire)\b", re.IGNORECASE)
_ANYONE_AVAILABLE_RE = re.compile(
    r"\b(is|are)\s+(anyone|anybody|somebody|someone)\s+available\b",
    re.IGNORECASE)

_NEEDISH_RE = re.compile(
    r"\bi\s+(need|want|am\s+looking\s+for)\b|\blooking\s+for\b"
    r"|\bfind(\s+me)?\b|\bsearch(ing)?\s+for\b|\bneed\b|\bwant\b"
    r"|\brequire[sd]?\b", re.IGNORECASE)

_OFFER_RE = re.compile(
    "|".join(_STRONG_OFFER) + r"|\bavailable\b|\boffer\b", re.IGNORECASE)

_IN_PLACE_RE = re.compile(
    r"\bin\s+([A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,2})")

_CATEGORY_TO_KIND = {
    "transport": "service",
    "job": "job",
    "service": "service",
    "supply": "supply",
    "collaboration": "collaboration",
    "other": "service",
}

_HIRING_RE = re.compile(r"\bhiring\b|\bhire\b|\bjoin\s+my\s+team\b",
                        re.IGNORECASE)

#: Employment-role mentions that mark an offer as a job posting even
#: when the category parser stayed generic ("need a frontend
#: developer" carries no job keyword but is clearly a job post).
_JOB_ROLE_RE = re.compile(
    r"\b(employee|staff|intern|developer|engineer)\b", re.IGNORECASE)

MAX_DESCRIPTION = 4000


class PostingError(ValueError):
    """User-facing posting failure."""


def classify_posting(text: str) -> str:
    """'offer' when the text provides something, else 'need'.

    Strong offer signals win; explicit need phrasing beats weak
    "available"; the review step lets the owner override either way.
    """
    clean = text or ""
    if _NEED_RENTAL_RE.search(clean) or _ANYONE_AVAILABLE_RE.search(clean):
        return "need"
    if _STRONG_OFFER_RE.search(clean):
        return "offer"
    if _NEEDISH_RE.search(clean):
        return "need"
    if _OFFER_RE.search(clean):
        return "offer"
    return "need"


def _title(text: str) -> str:
    clean = " ".join((text or "").split())
    for end in (". ", "! ", "? "):
        cut = clean.find(end)
        if 0 < cut <= 80:
            clean = clean[:cut]
            break
    clean = clean.rstrip(".!?").strip()
    if len(clean) > 80:
        clean = clean[:77].rstrip() + "..."
    return clean or "(untitled)"


def _in_place(text: str) -> str:
    match = _IN_PLACE_RE.search(text or "")
    return match.group(1).strip(" .") if match else ""


def parse_posting(text: str) -> dict:
    """Infer post fields from one description. Pure + deterministic."""
    clean = " ".join((text or "").split())
    if not clean:
        raise PostingError("Please describe what you'd like to post.")
    if len(clean) > MAX_DESCRIPTION:
        raise PostingError(
            f"Please keep descriptions under {MAX_DESCRIPTION} characters.")
    parsed = parse_need(clean)
    reqs = parsed["requirements"]
    location = (reqs.get("destination") or reqs.get("origin")
                or _in_place(clean))
    kind = classify_posting(clean)
    category = parsed["category"]
    if kind == "offer" and category == "other" and (
            _HIRING_RE.search(clean) or _JOB_ROLE_RE.search(clean)):
        category = "job"
    salary_min = reqs.get("salary_min")
    salary_max = reqs.get("salary_max")
    return {
        "kind": kind,
        "title": _title(clean),
        "description": clean,
        "category": category,
        "skills": list(reqs.get("skills", [])),
        "location": location,
        "budget_max": reqs.get("budget_max"),
        "salary_min": salary_min if salary_min is not None else "",
        "salary_max": salary_max if salary_max is not None else "",
        "when_text": reqs.get("when_text", ""),
        "quantity": parsed["quantity"] or 1,
    }


def publish_offer(conn: Connection, *, owner_user_id: int,
                  title: str, description: str, skills: list,
                  location: str, category: str,
                  salary_min: float | None = None,
                  salary_max: float | None = None,
                  expires_at: str | None = None) -> int:
    """Persist an offer and trigger real discovery. Returns opp id."""
    title = (title or "").strip()[:200]
    if not title:
        raise PostingError("Please give the offer a title.")
    clean_category = (category or "").strip().lower()[:40]
    if _HIRING_RE.search(f"{title} {description}") \
            or _JOB_ROLE_RE.search(f"{title} {description}") \
            or clean_category == "job":
        kind = "job"
        if not clean_category:
            clean_category = "job"
    else:
        kind = _CATEGORY_TO_KIND.get(clean_category or "other", "service")
    attributes: dict = {}
    if salary_min is not None:
        attributes["salary_min"] = salary_min
    if salary_max is not None:
        attributes["salary_max"] = salary_max
    opp_id = create_opportunity(
        conn, owner_user_id=owner_user_id, kind=kind,
        title=title, description=(description or "")[:MAX_DESCRIPTION],
        required_skills=[s.strip()[:60] for s in (skills or [])
                         if str(s).strip()][:20],
        location=(location or "").strip()[:120],
        category=clean_category, attributes=attributes or None,
        expires_at=expires_at)
    conn.commit()
    matcher.trigger_matching(conn, "opportunity_saved", "opportunity",
                             opp_id)
    return opp_id


def publish_need(conn: Connection, *, twin_id: int, owner_user_id: int,
                 need_text: str, category: str, skills: list,
                 location: str, quantity: int = 1,
                 expires_at: str | None = None,
                 salary_min: float | None = None,
                 salary_max: float | None = None) -> int:
    """Persist a need as an active search and trigger discovery."""
    clean = (need_text or "").strip()[:2000]
    if not clean:
        raise PostingError("Please describe what you need.")
    # Start from what the description itself states (origin, when,
    # budget, salary, ...), then let the review fields override.
    requirements = dict(parse_need(clean)["requirements"])
    if skills:
        requirements["skills"] = [str(s).strip()[:60] for s in skills
                                  if str(s).strip()][:10]
    if location:
        requirements["destination"] = location.strip()[:120]
    if salary_min is not None:
        requirements["salary_min"] = salary_min
    if salary_max is not None:
        requirements["salary_max"] = salary_max
    if expires_at is None:
        expires_at = matcher.plus_minutes(
            matcher.now_stamp(), 10080)  # 1 week default
    req_id, _ = need_engine.create_need(
        conn, twin_id=twin_id, owner_user_id=owner_user_id,
        category=(category or "other").strip()[:40] or "other",
        need_text=clean, requirements=requirements,
        idempotency_key=uuid.uuid4().hex, expires_at=expires_at,
        quantity_requested=quantity or 1)
    need_engine.confirm_request(conn, req_id, owner_user_id)
    matcher.trigger_matching(conn, "request_saved", "request", req_id)
    return req_id
