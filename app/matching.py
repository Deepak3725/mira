"""Deterministic opportunity matching (zero AI calls).

Scores are rough, explainable fit indicators on a 0-100 scale. They are NOT
scientifically validated success probabilities -- every UI surface must label
them as rough fit indicators (see templates).

Formula:
  skills      60 pts  -- share of required skills the seeker has
  experience  20 pts  -- preferred years met (or proportionally partial)
  location    20 pts  -- exact match, remote bridge, or partial

The engine knows nothing about specific users; it scores a SeekerProfile
against plain opportunity attributes, so it works for any future user or
opportunity kind.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from sqlite3 import Connection, Row
from typing import Any

from . import privacy
from .db import list_opportunities


def _now_stamp() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

SKILL_WEIGHT = 60.0
EXPERIENCE_WEIGHT = 20.0
LOCATION_WEIGHT = 20.0
REMOTE_PARTIAL_POINTS = 8.0
UNKNOWN_LOCATION_POINTS = 10.0


def _norm(value: Any) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def _json_list(raw: Any) -> list:
    try:
        parsed = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return [s for s in parsed if isinstance(s, str)] if isinstance(parsed, list) else []


@dataclass(frozen=True)
class SeekerProfile:
    """What a twin is looking for, parsed from its profile JSON."""

    skills: tuple[str, ...] = ()
    experience_years: int | float | None = None
    location: str | None = None
    open_to_remote: bool = False

    @classmethod
    def from_profile(cls, profile: dict[str, Any]) -> "SeekerProfile":
        raw_skills = profile.get("skills", []) or []
        skills = tuple(
            s.strip()
            for s in raw_skills
            if isinstance(s, str) and s.strip()
        )
        years = profile.get("experience_years")
        if (isinstance(years, bool)
                or not isinstance(years, (int, float))
                or not math.isfinite(years)
                or years < 0):
            # Garbage in, treated as unknown: negative years would
            # subtract points and NaN would crash scoring.
            years = None
        location = profile.get("location")
        if not isinstance(location, str) or not location.strip():
            location = None
        return cls(
            skills=skills,
            experience_years=years,
            location=location.strip() if location else None,
            open_to_remote=bool(profile.get("open_to_remote", False)),
        )

    def normalized_skills(self) -> set[str]:
        return {_norm(s) for s in self.skills if _norm(s)}


@dataclass(frozen=True)
class MatchResult:
    score: int
    matching_skills: tuple[str, ...]
    missing_skills: tuple[str, ...]
    explanation: str
    skill_points: float
    experience_points: float
    location_points: float


def score_opportunity(
    seeker: SeekerProfile,
    *,
    required_skills: list[str] | None,
    preferred_years: int | float | None,
    location: str | None,
    remote_ok: bool,
) -> MatchResult:
    """Score one opportunity for a seeker. Pure function, fully deterministic."""
    required = [s.strip() for s in (required_skills or []) if isinstance(s, str) and s.strip()]
    have = seeker.normalized_skills()
    matching = tuple(s for s in required if _norm(s) in have)
    missing = tuple(s for s in required if _norm(s) not in have)

    if not required:
        skill_points = SKILL_WEIGHT
        skill_note = "No specific skills required"
    else:
        skill_points = SKILL_WEIGHT * len(matching) / len(required)
        skill_note = f"Matches {len(matching)} of {len(required)} required skills"
        if matching:
            skill_note += f" ({', '.join(matching)})"
        if missing:
            skill_note += f". Missing: {', '.join(missing)}"

    if preferred_years is None or preferred_years <= 0:
        exp_points, exp_note = EXPERIENCE_WEIGHT, "No experience preference"
    elif seeker.experience_years is None:
        exp_points, exp_note = 0.0, "Experience not listed"
    elif seeker.experience_years >= preferred_years:
        exp_points = EXPERIENCE_WEIGHT
        exp_note = f"Meets preferred {preferred_years:g}+ years"
    else:
        exp_points = EXPERIENCE_WEIGHT * seeker.experience_years / preferred_years
        exp_note = (
            f"Below preferred {preferred_years:g} years (has {seeker.experience_years:g})"
        )

    opp_loc, seeker_loc = _norm(location), _norm(seeker.location)
    if opp_loc and seeker_loc and opp_loc == seeker_loc:
        loc_points = LOCATION_WEIGHT
        loc_note = f"Location matches ({location.strip()})"
    elif not opp_loc or not seeker_loc:
        loc_points, loc_note = UNKNOWN_LOCATION_POINTS, "Location unknown"
    elif remote_ok and seeker.open_to_remote:
        loc_points, loc_note = LOCATION_WEIGHT, "Remote-friendly for both sides"
    elif remote_ok or seeker.open_to_remote:
        loc_points, loc_note = REMOTE_PARTIAL_POINTS, "Partial location fit"
    else:
        loc_points = 0.0
        loc_note = f"Location differs ({location.strip()} vs {seeker.location})"

    total = skill_points + exp_points + loc_points
    score = max(0, min(100, int(round(total))))
    explanation = f"{skill_note}. {exp_note}. {loc_note}."
    return MatchResult(
        score=score,
        matching_skills=matching,
        missing_skills=missing,
        explanation=explanation,
        skill_points=round(skill_points, 2),
        experience_points=round(exp_points, 2),
        location_points=round(loc_points, 2),
    )


def find_matches(
    conn: Connection,
    twin: Row,
    *,
    skills_filter: list[str] | None = None,
    location_filter: str | None = None,
    status: str = "open",
    include_own: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Reusable discovery service: score + filter + rank opportunities.

    Returns only public opportunity fields plus owner twin identity (no
    emails, phones, or other private data). Sorted by score desc, id asc.
    """
    # Discovery input is the twin's AUTHORIZED public view only, and
    # routes score the viewer's OWN twin only (see pages.py). Private
    # fields can never leak through explanations, and nobody's twin is
    # scored for anyone else.
    seeker = SeekerProfile.from_profile(privacy.twin_public_view(twin))

    wanted_skills = {_norm(s) for s in (skills_filter or []) if _norm(s)}
    loc_filter = _norm(location_filter)

    results: list[dict[str, Any]] = []
    now = _now_stamp()
    for opp in list_opportunities(conn, status=status):
        if not include_own and opp["owner_user_id"] == twin["user_id"]:
            continue
        if status == "open" and offer_expired(opp, now):
            continue
        required = _json_list(opp["required_skills_json"])
        if wanted_skills and not (wanted_skills & {_norm(s) for s in required}):
            continue
        if loc_filter:
            if loc_filter == "remote":
                if not opp["remote_ok"]:
                    continue
            elif loc_filter not in _norm(opp["location"]):
                continue
        match = score_opportunity(
            seeker,
            required_skills=required,
            preferred_years=opp["preferred_experience_years"],
            location=opp["location"],
            remote_ok=bool(opp["remote_ok"]),
        )
        results.append(
            {
                "opportunity": {
                    "id": opp["id"],
                    "kind": opp["kind"],
                    "title": opp["title"],
                    "description": opp["description"],
                    "required_skills": required,
                    "preferred_experience_years": opp["preferred_experience_years"],
                    "location": opp["location"],
                    "remote_ok": bool(opp["remote_ok"]),
                    "status": opp["status"],
                },
                "owner": {
                    "name": opp["owner_name"],
                    "role": opp["owner_role"],
                    "twin_display_name": opp["owner_twin_name"],
                    "twin_avatar_seed": opp["owner_avatar_seed"],
                },
                "match": match,
            }
        )
    results.sort(key=lambda r: (-r["match"].score, r["opportunity"]["id"]))
    return results[: max(0, limit)]


# ---------------------------------------------------------------------------
# Bot-to-bot provider matching (Phase 1). Scores seeker REQUESTS against
# provider OFFERINGS. Same rules as above: deterministic, zero AI, public
# data only. Score: price fit 40 + place fit 30 + availability 15 + tags 15.
# ---------------------------------------------------------------------------

PRICE_WEIGHT = 40.0
PLACE_WEIGHT = 30.0
AVAIL_WEIGHT = 15.0
TAG_WEIGHT = 15.0


def _as_dict(raw: Any) -> dict:
    try:
        parsed = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _offer_price(attrs: dict) -> float | None:
    for key in ("price", "price_max", "price_min"):
        value = attrs.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value >= 0:
            return float(value)
    return None


def _salary_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0:
        return None
    return float(value)


def offer_expired(opp: Row, now: str) -> bool:
    """True when an offer's search duration has elapsed (TEXT/datetime)."""
    try:
        value = opp["expires_at"]
    except (KeyError, IndexError, TypeError):
        return False
    if not value:
        return False
    if hasattr(value, "strftime"):
        try:
            value = str(value.strftime("%Y-%m-%d %H:%M:%S"))
        except (ValueError, TypeError):
            return False
    return str(value)[:19] <= str(now)[:19]


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s.strip() for s in value if isinstance(s, str) and s.strip()]
    return []


def provider_summary_for_peer(requirements: dict) -> dict:
    """Request facts a contacted provider twin may see.

    The seeker's budget ceiling is NEVER shared -- it stays a private
    reservation the seeker side filters against. Providers quote; the
    seeker's twin compares.
    """
    return {k: v for k, v in (requirements or {}).items() if k != "budget_max"}


@dataclass(frozen=True)
class ProviderMatch:
    score: int
    explanation: str
    price_points: float
    place_points: float
    avail_points: float
    tag_points: float
    excluded_reason: str = ""


def score_provider(requirements: dict, attributes: dict) -> ProviderMatch:
    """Score one offering against request requirements. Pure + deterministic."""
    req = requirements or {}
    attrs = attributes or {}

    budget = req.get("budget_max")
    if isinstance(budget, bool) or not isinstance(budget, (int, float)):
        budget = None
    price = _offer_price(attrs)
    if budget is not None and price is not None and price > budget:
        return ProviderMatch(
            score=0, explanation="", price_points=0.0, place_points=0.0,
            avail_points=0.0, tag_points=0.0,
            excluded_reason=f"Over budget ({price:g} > {budget:g}).")
    req_min = _salary_number(req.get("salary_min"))
    req_max = _salary_number(req.get("salary_max"))
    off_min = _salary_number(attrs.get("salary_min"))
    off_max = _salary_number(attrs.get("salary_max"))
    # Disjoint salary ranges never match; figures stay out of the reason
    # because match reasons surface to both sides.
    if req_min is not None and off_max is not None and req_min > off_max:
        return ProviderMatch(
            score=0, explanation="", price_points=0.0, place_points=0.0,
            avail_points=0.0, tag_points=0.0,
            excluded_reason="Salary expectation above the offered range.")
    if off_min is not None and req_max is not None and off_min > req_max:
        return ProviderMatch(
            score=0, explanation="", price_points=0.0, place_points=0.0,
            avail_points=0.0, tag_points=0.0,
            excluded_reason="Salary offer above the stated range.")
    if budget is None or price is None:
        price_points, price_note = PRICE_WEIGHT / 2, "Price fit unknown"
    else:
        price_points, price_note = PRICE_WEIGHT, f"Within budget ({price:g} ≤ {budget:g})"

    endpoints = {_norm(req.get("origin")), _norm(req.get("destination"))} - {""}
    area = {_norm(s) for s in _str_list(attrs.get("service_area"))} - {""}
    loc = _norm(attrs.get("location"))
    if loc:
        area.add(loc)
    if not endpoints or not area:
        place_points, place_note = PLACE_WEIGHT / 2, "Coverage unknown"
    elif endpoints & area == endpoints:
        place_points, place_note = PLACE_WEIGHT, "Covers the route"
    elif endpoints & area:
        place_points, place_note = PLACE_WEIGHT / 2, "Partially covers the route"
    else:
        place_points, place_note = 0.0, "Outside service area"

    when = _norm(req.get("when_text")) + " " + _norm(req.get("when_iso"))
    avail = " ".join(_str_list(attrs.get("availability"))).lower()
    if not when.strip() or not avail:
        avail_points, avail_note = AVAIL_WEIGHT / 2, "Availability unknown"
    elif "24/7" in avail or "anytime" in avail or "always" in avail:
        avail_points, avail_note = AVAIL_WEIGHT, "Available anytime"
    elif set(when.split()) & set(avail.split()):
        avail_points, avail_note = AVAIL_WEIGHT, "Available at the requested time"
    else:
        avail_points, avail_note = 0.0, "Availability unclear"

    wanted = {_norm(s) for s in _str_list(req.get("skills"))} - {""}
    tags = {_norm(s) for s in _str_list(attrs.get("tags"))} - {""}
    if not wanted:
        tag_points, tag_note = TAG_WEIGHT, "No specific requirements"
    elif not tags:
        tag_points, tag_note = TAG_WEIGHT / 2, "Tags unknown"
    else:
        hit = wanted & tags
        tag_points = TAG_WEIGHT * len(hit) / len(wanted)
        tag_note = f"Matches {len(hit)} of {len(wanted)} tags"

    total = price_points + place_points + avail_points + tag_points
    explanation = f"{price_note}. {place_note}. {avail_note}. {tag_note}."
    return ProviderMatch(
        score=max(0, min(100, int(round(total)))), explanation=explanation,
        price_points=round(price_points, 2), place_points=round(place_points, 2),
        avail_points=round(avail_points, 2), tag_points=round(tag_points, 2))


def find_providers(
    conn: Connection,
    req: Row,
    *,
    limit: int = 10,
    candidate_cap: int = 50,
) -> list[dict[str, Any]]:
    """Provider offerings relevant to one request. Public data only.

    Category is a label, never a gate: every open offering is scored
    on price/place/availability/tags fit, so arbitrary needs ("build
    my website") still discover relevant posts. The candidate cap
    bounds cost as offerings grow. Sorted by fit desc, id asc for
    retrieval; the UI shows matching facts with no scores and no
    verdicts (hackathon compliance: providers are never ranked as
    applicants).
    """
    requirements = _as_dict(req["requirements_json"])
    results: list[dict[str, Any]] = []
    now = _now_stamp()
    for opp in list_opportunities(conn, status="open",
                                  limit=candidate_cap):
        if opp["owner_user_id"] == req["owner_user_id"]:
            continue
        if offer_expired(opp, now):
            continue
        # Skill-based offerings (jobs, gigs) carry their tags as
        # required skills; surface them to the tag signal instead
        # of scoring every skill-need as "tags unknown".
        attributes = _offering_attributes(opp)
        match = score_provider(requirements, attributes)
        if match.excluded_reason:
            continue
        results.append(
            {
                "offering": {
                    "id": opp["id"],
                    "kind": opp["kind"],
                    "category": opp["category"],
                    "title": opp["title"],
                    "description": opp["description"],
                    "location": opp["location"],
                    "attributes": attributes,
                },
                "owner": {
                    "name": opp["owner_name"],
                    "role": opp["owner_role"],
                    "twin_display_name": opp["owner_twin_name"],
                    "twin_avatar_seed": opp["owner_avatar_seed"],
                },
                "match": match,
            }
        )
    results.sort(key=lambda r: (-r["match"].score, r["offering"]["id"]))
    return results[: max(0, limit)]


# ---------------------------------------------------------------------------
# Reverse discovery: open NEEDS relevant to one twin (universal Find
# Something). Mirrors find_providers: a twin discovers other owners'
# shared requests it could help with, scored against its own offerings
# plus plain word overlap, never gated on category. Only shared
# (owner-confirmed, not withdrawn) needs surface, and the seeker's
# budget ceiling is stripped before display.
# ---------------------------------------------------------------------------

_KEYWORD_STOP = frozenset({
    "a", "an", "the", "and", "or", "but", "for", "with", "from", "that",
    "this", "these", "those", "are", "was", "were", "have", "has", "had",
    "will", "would", "could", "should", "need", "needs", "needing",
    "want", "wants", "looking", "find", "please", "someone", "somebody",
    "person", "people", "help", "who", "can", "into", "out", "about",
    "your", "you", "our", "their", "his", "her", "its", "not",
})


def _keywords(text: Any) -> set[str]:
    import re as _re

    words = _re.findall(r"[a-z0-9+#.]+", str(text or "").lower())
    return {w.strip("+#.") for w in words
            if len(w.strip("+#.")) >= 3 and w not in _KEYWORD_STOP}


@dataclass(frozen=True)
class NeedMatch:
    score: int
    explanation: str
    offer_points: float
    text_points: float


def _offering_attributes(opp: Row) -> dict:
    attributes = _as_dict(opp["attributes_json"])
    if not attributes.get("location") and opp["location"]:
        attributes = dict(attributes, location=opp["location"])
    if not _str_list(attributes.get("tags")):
        required = _json_list(opp["required_skills_json"])
        if required:
            attributes = dict(attributes, tags=required)
    return attributes


def _offering_words(opp: Row) -> set[str]:
    words = _keywords(opp["title"]) | _keywords(opp["description"])
    for tag in _str_list(_as_dict(opp["attributes_json"]).get("tags")):
        words |= _keywords(tag)
    for skill in _json_list(opp["required_skills_json"]):
        words |= _keywords(skill)
    return words


def find_needs(
    conn: Connection,
    twin: Row,
    *,
    limit: int = 10,
    candidate_cap: int = 50,
) -> list[dict[str, Any]]:
    """Others' shared needs this twin could help with. Public data only.

    Two signals, both deterministic: (1) the twin's own offerings
    scored against the need's requirements (best offering wins, same
    price/place/availability/tags rules as find_providers); (2) word
    overlap between the need text and the twin's offerings/profile.
    Twins with no offerings still match on profile words. Sorted by
    fit desc, id asc; the UI shows matching facts with no scores and
    no verdicts.
    """
    from .db import list_opportunities, list_shared_requests
    from .requests import request_title

    my_offerings = [o for o in list_opportunities(conn, status="open")
                    if o["owner_user_id"] == twin["user_id"]]
    seeker = SeekerProfile.from_profile(privacy.twin_public_view(twin))
    profile_words = _keywords(" ".join(seeker.skills))
    if seeker.location:
        profile_words |= _keywords(seeker.location)
    my_words: set[str] = set(profile_words)
    for opp in my_offerings:
        my_words |= _offering_words(opp)

    results: list[dict[str, Any]] = []
    for req in list_shared_requests(
            conn, exclude_user_id=twin["user_id"], limit=candidate_cap):
        requirements = _as_dict(req["requirements_json"])
        best: ProviderMatch | None = None
        via: Row | None = None
        for opp in my_offerings:
            scored = score_provider(
                requirements, _offering_attributes(opp))
            if scored.excluded_reason:
                continue
            if best is None or scored.score > best.score:
                best, via = scored, opp
        offer_points = float(best.score) if best else 0.0

        need_words = _keywords(req["need_text"])
        for skill in _str_list(requirements.get("skills")):
            need_words |= _keywords(skill)
        overlap = need_words & my_words if my_words else set()
        if need_words and my_words:
            text_points = 100.0 * len(overlap) / len(need_words)
        else:
            text_points = 0.0

        if my_offerings:
            total = 0.7 * offer_points + 0.3 * text_points
        else:
            total = text_points
        score = max(0, min(100, int(round(total))))
        if score <= 0:
            continue
        notes = []
        if best is not None and via is not None:
            notes.append(f"Fits your offering '{via['title']}'"
                         f" ({best.explanation})")
        if overlap:
            shown = ", ".join(sorted(overlap)[:5])
            notes.append(f"Shares: {shown}")
        elif not my_offerings:
            notes.append("Word overlap with your twin profile")
        results.append(
            {
                "need": {
                    "id": req["id"],
                    "title": request_title(req["need_text"]),
                    "need_text": req["need_text"],
                    "category": req["category"],
                    "status": req["status"],
                    "requirements": provider_summary_for_peer(
                        requirements),
                },
                "seeker": {
                    "name": req["owner_name"],
                    "role": req["owner_role"],
                    "twin_display_name": req["twin_name"],
                    "twin_avatar_seed": req["twin_avatar_seed"],
                },
                "via_offering": (
                    {"id": via["id"], "title": via["title"]}
                    if via is not None else None
                ),
                "match": NeedMatch(
                    score=score,
                    explanation="; ".join(notes) or "Possibly relevant.",
                    offer_points=round(offer_points, 2),
                    text_points=round(text_points, 2)),
            }
        )
    results.sort(key=lambda r: (-r["match"].score, r["need"]["id"]))
    return results[: max(0, limit)]
