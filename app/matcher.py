"""Event-driven persistent-search matching (no polling for matches).

A confirmed request is a persistent search: it matches eligible posts
immediately at creation, and every later post-save re-evaluates active
searches inline (same HTTP request). A ``match_queue`` row is written in
the same transaction as the post, so a crash between commit and inline
processing still recovers via :func:`process_due` — retries are
idempotent (unique match pairs, notify-only-on-insert).

Deadlines use UTC ``YYYY-MM-DD HH:MM:SS`` stamps; string comparison is
chronological in that format. ``process_due`` takes an injectable clock
so tests never wait out real deadlines.
"""

from __future__ import annotations

import datetime as _dt
import re
from sqlite3 import Connection, Row
from typing import Any

from . import requests as need_engine
from .db import (
    count_accepted_for_search,
    create_match,
    create_notification,
    enqueue_match_event,
    get_match,
    get_match_by_pair,
    get_opportunity,
    get_request,
    get_twin,
    get_twin_by_user,
    list_active_searches,
    list_matches_for_search,
    list_opportunities,
    list_shared_requests,
    set_queue_status,
    claim_due_match_events,
    update_match,
    update_request,
)
from .matching import (
    _keywords,
    _offering_attributes,
    score_provider,
)

#: Owner-facing search durations (form value -> minutes, 0 = no deadline).
SEARCH_DURATIONS = (("none", 0), ("1h", 60), ("2h", 120), ("5h", 300),
                    ("1d", 1440), ("1w", 10080))

MAX_QUEUE_ATTEMPTS = 5
RETRY_BASE_SECONDS = 60


class MatcherError(Exception):
    """User-facing matching error (permission or state problem)."""


def now_stamp(now: _dt.datetime | None = None) -> str:
    moment = now or _dt.datetime.now(_dt.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    return moment.astimezone(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def plus_minutes(base: str, minutes: int) -> str:
    moment = _dt.datetime.strptime(base, "%Y-%m-%d %H:%M:%S")
    return (moment + _dt.timedelta(minutes=minutes)).strftime(
        "%Y-%m-%d %H:%M:%S")


#: A need phrased as available supply ("I am available to tutor Java",
#: "my car is available for rent") behaves as the supply side.
_SUPPLY_RE = re.compile(
    r"\bavailable\b|\bi offer\b|\bwe offer\b|\bfor hire\b|\bfor rent\b"
    r"|\bmy \w+ is available\b",
    re.IGNORECASE,
)

#: A need seeking people ("find me four passengers") is also supply-side:
#: the poster provides the trip/service and wants customers.
_SEEKS_PEOPLE_RE = re.compile(
    r"\bpassengers?\b|\briders?\b|\bcustomers?\b|\bclients?\b|\bguests?\b"
    r"|\bmembers?\b|\bbuyers?\b|\btenants?\b|\bvolunteers?\b",
    re.IGNORECASE,
)


def need_direction(text: str) -> str:
    """Classify a need post as supply-side or demand-side."""
    text = text or ""
    if _SUPPLY_RE.search(text) or _SEEKS_PEOPLE_RE.search(text):
        return "supply"
    return "demand"


def _keyword_plus(text: Any) -> set[str]:
    """Keywords plus joined adjacent pairs (full-stack -> fullstack)
    and naive singulars (passengers -> passenger). Deterministic,
    recall-oriented; precision still comes from the two-word rule."""
    words = [w for w in re.findall(r"[a-z0-9]+", str(text or "").lower())
             if len(w) >= 2]
    out = set(_keywords(text))
    for word in _keywords(text):
        singular = word[:-1]
        if word.endswith("s") and len(word) > 4 \
                and singular not in out:
            out.add(singular)
    for first, second in zip(words, words[1:]):
        joined = first + second
        if len(joined) >= 5:
            out.add(joined)
    return out


def _as_dict(raw: Any) -> dict:
    import json as _json

    try:
        parsed = _json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s.strip() for s in value
                if isinstance(s, str) and s.strip()]
    return []


def _norm(value: Any) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def _endpoints(requirements: dict) -> set[str]:
    return {_norm(requirements.get("origin")),
            _norm(requirements.get("destination"))} - {""}


def _counterpart_facts(conn: Connection, counterpart_type: str,
                       counterpart_id: int) -> dict | None:
    """Normalized (requirements, text, direction, owner, twin) or None."""
    if counterpart_type == "opportunity":
        opp = get_opportunity(conn, counterpart_id)
        if opp is None or opp["status"] != "open":
            return None
        text = f"{opp['title']} {(opp['description'] or '')}"
        reqs: dict = {}
        places = parse_places(text + " " + (opp["location"] or ""))
        if places[0]:
            reqs["origin"] = places[0]
        if places[1]:
            reqs["destination"] = places[1]
        peer_twin = get_twin_by_user(conn, opp["owner_user_id"])
        return {"requirements": reqs, "text": text, "direction": "supply",
                "attrs": _offering_attributes(opp),
                "owner_user_id": opp["owner_user_id"],
                "twin": peer_twin, "title": opp["title"],
                "skills": [s for s in
                           _as_list(opp["required_skills_json"])],
                "kind": "offer"}
    req = get_request(conn, counterpart_id)
    if req is None or not need_engine.is_shared(req):
        return None
    reqs = _as_dict(req["requirements_json"])
    peer_twin = get_twin_by_user(conn, req["owner_user_id"])
    return {"requirements": reqs, "text": req["need_text"],
            "direction": need_direction(req["need_text"]),
            "attrs": {"tags": _str_list(reqs.get("skills"))},
            "owner_user_id": req["owner_user_id"], "twin": peer_twin,
            "title": need_engine.request_title(req["need_text"]),
            "skills": _str_list(reqs.get("skills")), "kind": "need"}


def _as_list(raw: Any) -> list:
    import json as _json

    try:
        parsed = _json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return [s for s in parsed if isinstance(s, str)] \
        if isinstance(parsed, list) else []


def parse_places(text: str) -> tuple[str | None, str | None]:
    """Best-effort origin/destination; delegates to the shared parser."""
    from .request_parse import parse_places as _shared

    return _shared(text or "")


def compatible(search_reqs: dict, search_text: str, search_direction: str,
               counter: dict) -> tuple[int, str] | None:
    """Score a search/counterpart pair, or None when incompatible.

    Gates (all must pass): opposite sides of the market (supply meets
    demand, never supply-supply or demand-demand), then at least one
    positive relevance signal — shared route, shared skill, two or more
    shared keywords, or a decisive structured fit. A single common
    word is never enough.
    """
    if search_direction == counter["direction"]:
        return None
    structured = score_provider(search_reqs, counter["attrs"])
    if structured.excluded_reason:
        return None
    search_eps = _endpoints(search_reqs)
    counter_eps = _endpoints(counter["requirements"])
    route_hit = bool(search_eps and counter_eps
                     and (search_eps & counter_eps))
    search_skills = {_norm(s) for s in _str_list(search_reqs.get("skills"))}
    counter_skills = {_norm(s) for s in counter["skills"]}
    skill_hit = bool(search_skills and counter_skills
                     and (search_skills & counter_skills))
    search_kw = _keyword_plus(search_text)
    counter_kw = _keyword_plus(counter["text"])
    shared = search_kw & counter_kw if search_kw and counter_kw else set()
    decisive = structured.score >= 70
    if not (route_hit or skill_hit or len(shared) >= 2 or decisive):
        return None
    text_points = (100.0 * len(shared) / len(search_kw)) if search_kw else 0.0
    score = max(0, min(100, int(round(
        0.5 * structured.score + 0.5 * text_points))))
    notes = []
    if route_hit:
        notes.append("same route: " + ", ".join(
            sorted(search_eps & counter_eps)[:3]))
    if skill_hit:
        notes.append("shares: " + ", ".join(
            sorted(search_skills & counter_skills)[:4]))
    elif shared:
        notes.append("shares: " + ", ".join(sorted(shared)[:5]))
    notes.append(structured.explanation)
    return score, _sanitize_reason("; ".join(notes))


def _sanitize_reason(reason: str) -> str:
    """Peer-safe match reason: keep the verdict, drop the figures.

    The structured explanation names exact budgets and prices, which
    must never cross to the other side (the seeker's ceiling stays
    private, exactly like provider_summary_for_peer).
    """
    clean = re.sub(r"Within budget [\d.,]+ \S+(\s*\(price [\d.,]+\))?\.?",
                   "Within budget.", reason)
    clean = re.sub(r"[Pp]rice [\d.,]+ is over budget [\d.,]+;"
                   r" counted partial\.",
                   "Price is a stretch; counted partial.", clean)
    clean = re.sub(r"No budget stated \(price [\d.,]+\)\.",
                   "No budget stated.", clean)
    return clean


def _notify(conn: Connection, user_id: int, kind: str, title: str,
            body: str = "", link: str = "") -> None:
    create_notification(conn, user_id=user_id, kind=kind, title=title,
                        body=body, link=link)


def _consider_pair(conn: Connection, search: Row, counterpart_type: str,
                   counterpart_id: int) -> int | None:
    """Evaluate one pair; returns the new match id, or None."""
    if counterpart_type == "request" and counterpart_id == search["id"]:
        return None
    counter = _counterpart_facts(conn, counterpart_type, counterpart_id)
    if counter is None:
        return None
    if counter["owner_user_id"] == search["owner_user_id"]:
        return None
    peer_twin = counter["twin"]
    if peer_twin is None or not peer_twin["is_public"]:
        # Auto-matches promise coordinatability; private twins cannot
        # be contacted, so they never auto-match (manual discovery
        # still lists the post itself).
        return None
    search_reqs = _as_dict(search["requirements_json"])
    own_twin = get_twin(conn, search["twin_id"])
    if own_twin is not None:
        # Reuse the searcher's approved-shared profile skills: the
        # twin knows its owner, but only through fields the owner
        # authorized for sharing (never invented, never private).
        from .privacy import twin_public_view

        approved = twin_public_view(own_twin).get("skills", [])
        merged = _str_list(search_reqs.get("skills"))
        for skill in _str_list(approved):
            if skill not in merged:
                merged.append(skill)
        if merged:
            search_reqs = dict(search_reqs, skills=merged)
    verdict = compatible(search_reqs, search["need_text"],
                         need_direction(search["need_text"]), counter)
    if verdict is None:
        return None
    score, reason = verdict
    existing = get_match_by_pair(conn, search["id"], counterpart_type,
                                 counterpart_id)
    if existing is not None:
        if existing["status"] in ("rejected", "withdrawn"):
            return None  # a human decision stands; never resurrect
        update_match(conn, existing["id"], score=score, reason=reason)
        return None
    match_id = create_match(
        conn, search_request_id=search["id"],
        counterpart_type=counterpart_type, counterpart_id=counterpart_id,
        score=score, reason=reason)
    if match_id is None:
        return None  # lost a race; the other writer won
    title = need_engine.request_title(search["need_text"])
    _notify(conn, search["owner_user_id"], "match",
            f"New match for '{title}'",
            f"{counter['title']} ({counter['kind']}) looks relevant.",
            f"/requests/{search['id']}")
    peer_word = "search" if counterpart_type == "opportunity" else "need"
    _notify(conn, counter["owner_user_id"], "match",
            f"Your {peer_word} '{counter['title'][:60]}' has interest",
            f"It matches {search['twin_name']}'s search '{title}'.",
            "/requests")
    if search["status"] == "open":
        update_request(conn, search["id"], status="matched")
    return match_id


def match_search(conn: Connection, search_id: int) -> dict[str, int]:
    """Match one active search against all eligible posts."""
    search = get_request(conn, search_id)
    if search is None or search["status"] not in need_engine.SHARED_STATUSES:
        return {"created": 0, "skipped": 0}
    created = skipped = 0
    for opp in list_opportunities(conn, status="open", limit=500):
        if opp["owner_user_id"] == search["owner_user_id"]:
            continue
        found = _consider_pair(conn, search, "opportunity", opp["id"])
        created += 1 if found else 0
        skipped += 0 if found else 1
    for req in list_shared_requests(conn,
                                    exclude_user_id=search["owner_user_id"],
                                    limit=500):
        found = _consider_pair(conn, search, "request", req["id"])
        created += 1 if found else 0
        skipped += 0 if found else 1
    conn.commit()
    return {"created": created, "skipped": skipped}


def match_offer(conn: Connection, opp_id: int) -> dict[str, int]:
    """Match one saved offer against all active searches."""
    opp = get_opportunity(conn, opp_id)
    if opp is None or opp["status"] != "open":
        return {"created": 0, "skipped": 0}
    created = skipped = 0
    for search in list_active_searches(conn):
        if search["owner_user_id"] == opp["owner_user_id"]:
            continue
        found = _consider_pair(conn, search, "opportunity", opp["id"])
        created += 1 if found else 0
        skipped += 0 if found else 1
    conn.commit()
    return {"created": created, "skipped": skipped}


def match_need_as_counterpart(conn: Connection, need_id: int) -> dict[str, int]:
    """Match one saved need against all active searches (need-need).

    A new need plays two roles: it is a search itself (match_search)
    and a potential counterpart for waiting searches (this half).
    """
    need = get_request(conn, need_id)
    if need is None or not need_engine.is_shared(need):
        return {"created": 0, "skipped": 0}
    created = skipped = 0
    for search in list_active_searches(conn):
        if search["id"] == need_id:
            continue
        if search["owner_user_id"] == need["owner_user_id"]:
            continue
        found = _consider_pair(conn, search, "request", need_id)
        created += 1 if found else 0
        skipped += 0 if found else 1
    conn.commit()
    return {"created": created, "skipped": skipped}


def process_event(conn: Connection, event: Row) -> dict[str, int]:
    """Run one queue event. Raises on failure (caller schedules retry)."""
    if event["event_type"] == "request_saved":
        first = match_search(conn, event["ref_id"])
        second = match_need_as_counterpart(conn, event["ref_id"])
        return {"created": first["created"] + second["created"],
                "skipped": first["skipped"] + second["skipped"]}
    if event["event_type"] == "opportunity_saved":
        return match_offer(conn, event["ref_id"])
    raise MatcherError(f"Unknown event {event['event_type']!r}.")


def trigger_matching(conn: Connection, event_type: str, ref_type: str,
                     ref_id: int) -> None:
    """Enqueue a match event and process it inline, best-effort.

    The queue row commits with the caller, so a crash still recovers
    via the sweeper; inline failures never break the save itself.
    Shared by every post-save path (requests, offers, chat, posts).
    """
    enqueue_match_event(conn, event_type=event_type, ref_type=ref_type,
                        ref_id=ref_id)
    conn.commit()
    try:
        process_due(conn, now_stamp(), limit=5)
    except Exception:  # noqa: BLE001 - sweeper retries later
        conn.rollback()


def expire_due(conn: Connection, now: str) -> int:
    """Expire past-deadline searches; history and matches preserved."""
    rows = list(conn.execute(
        "SELECT id, owner_user_id, need_text FROM requests"
        " WHERE status IN ('open', 'matched', 'chatting', 'decided')"
        " AND expires_at IS NOT NULL AND expires_at <= ?",
        (now,)))
    for row in rows:
        update_request(conn, row["id"], status="expired")
        _notify(conn, row["owner_user_id"], "expired",
                "Your search period ended",
                f"'{need_engine.request_title(row['need_text'])}' expired."
                " Extend it to keep searching.",
                f"/requests/{row['id']}")
    if rows:
        conn.commit()
    return len(rows)


def process_due(conn: Connection, now: str | None = None,
                limit: int = 25) -> dict[str, int]:
    """Sweep: expirations plus queued/failed-retry match events."""
    stamp = now or now_stamp()
    counts = {"expired": 0, "events": 0, "matches": 0, "failed": 0}
    counts["expired"] = expire_due(conn, stamp)
    for event in claim_due_match_events(conn, stamp, limit):
        counts["events"] += 1
        try:
            result = process_event(conn, event)
            counts["matches"] += result.get("created", 0)
            set_queue_status(conn, event["id"], "done")
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - retry, never lose
            attempts = (event["attempts"] or 0) + 1
            if attempts >= MAX_QUEUE_ATTEMPTS:
                # Terminal: kept for inspection, never re-claimed
                # (claim only takes pending/failed).
                set_queue_status(conn, event["id"], "dead",
                                 attempts=attempts,
                                 last_error=f"{type(exc).__name__}: {exc}")
            else:
                backoff = RETRY_BASE_SECONDS * (2 ** min(attempts, 4))
                set_queue_status(
                    conn, event["id"], "failed", attempts=attempts,
                    next_retry_at=plus_minutes(stamp, backoff // 60 or 1),
                    last_error=f"{type(exc).__name__}: {exc}")
            conn.commit()
            counts["failed"] += 1
    return counts


def fulfillment(conn: Connection, search_id: int) -> dict[str, int]:
    """Quantity accounting. Only owner-accepted matches count, and each
    counterpart pair counts once (unique pairs make double-counting
    impossible)."""
    search = get_request(conn, search_id)
    requested = 1
    if search is not None:
        try:
            requested = max(1, int(search["quantity_requested"] or 1))
        except (TypeError, ValueError):
            requested = 1
    accepted = count_accepted_for_search(conn, search_id)
    return {"requested": requested, "accepted": accepted,
            "remaining": max(0, requested - accepted)}


def _recheck_fulfillment(conn: Connection, search_id: int) -> None:
    search = get_request(conn, search_id)
    if search is None:
        return
    numbers = fulfillment(conn, search_id)
    if numbers["remaining"] == 0 and search["status"] in (
            "open", "matched", "chatting", "decided"):
        update_request(conn, search_id, status="fulfilled")
        _notify(conn, search["owner_user_id"], "fulfilled",
                "Your search is fulfilled",
                f"'{need_engine.request_title(search['need_text'])}'"
                f" reached {numbers['requested']} accepted"
                f" match(es).",
                f"/requests/{search_id}")
    elif numbers["remaining"] > 0 and search["status"] == "fulfilled":
        update_request(conn, search_id, status="open")
        _notify(conn, search["owner_user_id"], "reopened",
                "Your search needs attention",
                "An accepted match was withdrawn; the search is active"
                " again.",
                f"/requests/{search_id}")


def _peer_user_id(conn: Connection, match: Row) -> int | None:
    if match["counterpart_type"] == "opportunity":
        opp = get_opportunity(conn, match["counterpart_id"])
        return opp["owner_user_id"] if opp else None
    req = get_request(conn, match["counterpart_id"])
    return req["owner_user_id"] if req else None


def _start_coordination_chat(conn: Connection, match: Row) -> int | None:
    """Open the twin chat for an accepted match (bounded, best-effort).

    Creation honors the engine's own guards (existing open chat reused,
    private peers refused); a refusal leaves the accepted match in
    place so the owners can coordinate manually.
    """
    from . import conversations as engine

    search = get_request(conn, match["search_request_id"])
    if search is None:
        return None
    peer_id = _peer_user_id(conn, match)
    if peer_id is None:
        return None
    peer_twin = get_twin_by_user(conn, peer_id)
    if peer_twin is None:
        return None
    try:
        conv_id, _ = engine.get_or_create_request_conversation(
            conn, twin_a_id=search["twin_id"],
            twin_b_id=peer_twin["id"],
            created_by_user_id=search["owner_user_id"],
            request_id=search["id"],
            opportunity_id=(match["counterpart_id"]
                            if match["counterpart_type"] == "opportunity"
                            else None))
        return conv_id
    except engine.ConversationError:
        return None


def accept_match(conn: Connection, match_id: int,
                 user_id: int | None) -> Row:
    """Record one side's acceptance. Only the search owner's acceptance
    moves a match to accepted (peer acceptance is recorded alongside);
    either side's rejection is terminal for the pair."""
    match = get_match(conn, match_id)
    if match is None:
        raise MatcherError("Match not found.")
    search = get_request(conn, match["search_request_id"])
    if search is None:
        raise MatcherError("Match not found.")
    peer_id = _peer_user_id(conn, match)
    if user_id != search["owner_user_id"] and user_id != peer_id:
        raise MatcherError("Only a participant can decide.")
    if match["status"] in ("rejected", "withdrawn"):
        raise MatcherError("This match was already decided.")
    if user_id == search["owner_user_id"]:
        numbers = fulfillment(conn, search["id"])
        if numbers["remaining"] <= 0 and not match["owner_accepted"]:
            _recheck_fulfillment(conn, search["id"])
            conn.commit()
            raise MatcherError("This search is already fulfilled.")
        update_match(conn, match_id, status="accepted",
                     owner_accepted=1)
        conv_id = _start_coordination_chat(conn, match)
        if peer_id is not None:
            _notify(conn, peer_id, "accepted",
                    "Your post was accepted",
                    f"{search['twin_name']} accepted the match.",
                    f"/conversations/{conv_id}" if conv_id
                    else "/requests")
        _recheck_fulfillment(conn, search["id"])
    else:
        update_match(conn, match_id, peer_accepted=1)
        _notify(conn, search["owner_user_id"], "accepted",
                "A counterpart is interested",
                "They accepted the match on their side.",
                f"/requests/{search['id']}")
    conn.commit()
    return get_match(conn, match_id)


def reject_match(conn: Connection, match_id: int,
                 user_id: int | None) -> Row:
    """Either side rejects; the pair never auto-matches again."""
    match = get_match(conn, match_id)
    if match is None:
        raise MatcherError("Match not found.")
    search = get_request(conn, match["search_request_id"])
    if search is None:
        raise MatcherError("Match not found.")
    peer_id = _peer_user_id(conn, match)
    if user_id != search["owner_user_id"] and user_id != peer_id:
        raise MatcherError("Only a participant can decide.")
    if match["status"] in ("rejected", "withdrawn"):
        conn.commit()
        return match
    was_counted = bool(match["owner_accepted"]
                       and match["status"] == "accepted")
    update_match(conn, match_id, status="rejected")
    other = peer_id if user_id == search["owner_user_id"] \
        else search["owner_user_id"]
    link = "/requests" if user_id == search["owner_user_id"] \
        else f"/requests/{search['id']}"
    if other is not None:
        _notify(conn, other, "rejected", "A match was declined",
                "The other side declined this match.", link)
    if was_counted:
        _recheck_fulfillment(conn, search["id"])
    conn.commit()
    return get_match(conn, match_id)


def withdraw_search_matches(conn: Connection, search_id: int) -> int:
    """Owner cancelled: withdraw live matches, notify accepted peers."""
    search = get_request(conn, search_id)
    count = 0
    for match in list_matches_for_search(conn, search_id):
        if match["status"] not in ("potential", "accepted"):
            continue
        update_match(conn, match["id"], status="withdrawn")
        count += 1
        if match["status"] == "accepted":
            peer_id = _peer_user_id(conn, match)
            if peer_id is not None:
                _notify(conn, peer_id, "withdrawn",
                        "A search was withdrawn",
                        "The search owner withdrew their request.",
                        "/requests")
    if count:
        conn.commit()
    return count


def extend_search(conn: Connection, search_id: int, user_id: int | None,
                  minutes: int) -> Row:
    """Owner extends an expired (or active) search by minutes."""
    search = get_request(conn, search_id)
    if search is None:
        raise MatcherError("Request not found.")
    if search["owner_user_id"] != user_id:
        raise MatcherError("Only the owner can extend this search.")
    if search["status"] not in ("expired", "open", "matched", "chatting",
                                "decided", "paused"):
        raise MatcherError("Only an expired or active search can extend.")
    minutes = max(1, min(525600, int(minutes)))
    update_request(conn, search_id, status="open",
                   expires_at=plus_minutes(now_stamp(), minutes))
    conn.commit()
    return get_request(conn, search_id)
