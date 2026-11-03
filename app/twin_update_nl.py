"""Natural-language twin updates (Part 2).

Parses commands like "remove Frontend Developer from my skills and
add Fullstack Developer" into field operations, applies them to the
owner's twin only, and reports before/after. One-step undo restores
the previous snapshot. Used only by typed text interactions.
"""

from __future__ import annotations

import json
import re

from .conversations import audit
from .db import get_twin, update_twin

#: field -> (kind, aliases). kind "list" holds de-duplicated items,
#: "text" holds a single string, "twin" lives on the twin row itself.
FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "skills": ("list", ("skill", "skills")),
    "interests": ("list", ("interest", "interests", "hobby",
                           "hobbies")),
        "requirements": ("list", ("requirement", "requirements")),
    "headline": ("text", ("headline", "title", "tagline",
                          "professional title")),
    "bio": ("text", ("bio", "about", "description", "background",
                     "summary")),
    "company": ("text", ("company", "employer", "workplace")),
    "location": ("text", ("location", "city", "based")),
    "availability": ("text", ("availability", "available")),
    "experience_years": ("number", ("experience years", "years of experience")),
    "tone": ("twin", ("tone", "style", "communication style")),
    "display_name": ("twin", ("name", "display name", "twin name")),
    "persona": ("twin", ("persona", "personality")),
    "role": ("text", ("professional role", "role")),
    "portfolio_url": ("text", ("portfolio link", "portfolio url")),
}

_SPLIT = re.compile(r"\s+(?:and|then)\s+|[;.]")


def is_update_command(text: str) -> bool:
    """Detect explicit profile-edit/undo wording, not job searches."""
    return bool(re.search(r"\b(?:update|edit|change)\s+my\s+(?:twin|profile)\b|\b(?:add|remove|delete|drop|clear|reset|set|replace|change)\b.{0,120}\bmy\s+(?:skills?|interests?|requirements?|headline|bio|company|location|availability|experience years|years of experience|role|portfolio|name|tone|persona)\b|\b(?:undo|revert)\s+(?:my\s+)?(?:last\s+)?(?:twin\s+)?update\b", text or "", re.I))


def field_from_words(text: str) -> str | None:
    lowered = f" {(text or '').lower()} "
    best: str | None = None
    best_len = 0
    for field, (_kind, aliases) in FIELDS.items():
        for alias in aliases:
            if f" {alias} " in lowered and len(alias) > best_len:
                best, best_len = field, len(alias)
    return best


def _values(text: str) -> list[str]:
    return [v.strip(" .") for v in (text or "").split(",")
            if v.strip(" .")][:30]


def parse_update(text: str, default_field: str | None = None) -> dict:
    """Parse one update command into ops. Pure, deterministic.

    Returns {"ops": [...], "errors": [...]}. Each op is
    {"op": add|remove|replace|set|clear, "field", "values",
    "target"}. ``needs_confirm`` marks destructive previews.
    ``default_field`` lets a follow-up turn ("add Go") inherit the
    field established by an earlier prompt.
    """
    cleaned = " ".join((text or "").split())
    cleaned = re.sub(r"^(?:update|edit|change) my (?:twin|profile)\s*[:,]?\s*", "", cleaned, flags=re.I)
    ops: list[dict] = []
    errors: list[str] = []
    segments = [s.strip(" .!?") for s in _SPLIT.split(cleaned)
                if s.strip(" .!?")]
    last_field: str | None = default_field
    for seg in segments:
        lowered = seg.lower()
        if re.match(r"^(update|change|edit)\s+my\s+twin\.?$", lowered):
            continue  # bare trigger, no operation
        op = _parse_segment(seg)
        if op is None and last_field is not None:
            # "remove X from my skills and add Y": Y inherits skills.
            op = _parse_bare(seg, last_field)
        if op is None:
            errors.append(_field_hint(seg))
        else:
            ops.append(op)
            last_field = op["field"]
    return {"ops": ops, "errors": errors}


def _field_hint(seg: str) -> str:
    if re.match(r"^add\s+.+", seg, re.IGNORECASE):
        return (f"I couldn't tell which list {seg!r} belongs to."
                " Say 'add it to my skills' (or interests,"
                " preferences).")
    return (f"I couldn't understand {seg!r}. Try 'add X to my"
            " skills' or 'set my headline to ...'.")


def _parse_bare(seg: str, field: str) -> dict | None:
    """Field-less follow-up ("... and add Y") inherits the last field."""
    match = re.match(r"^add\s+(.+)$", seg, re.IGNORECASE)
    if match:
        return {"op": "add", "field": field,
                "values": _values(match.group(1)), "target": ""}
    match = re.match(r"^(?:remove|delete|drop)\s+(.+)$", seg,
                     re.IGNORECASE)
    if match:
        return {"op": "remove", "field": field,
                "values": _values(match.group(1)), "target": ""}
    match = re.match(r"^(?:replace|change)\s+(.+?)\s+(?:with|to)\s+"
                     r"(.+)$", seg, re.IGNORECASE)
    if match:
        return {"op": "replace", "field": field,
                "target": match.group(1).strip(" ."),
                "values": _values(match.group(2))}
    return None


def _parse_segment(seg: str) -> dict | None:
    lowered = seg.lower()
    # replace X with Y in/of <field>; change X to Y in <field>
    match = re.search(
        r"(?:replace|change|swap)\s+(.+?)\s+(?:with|to)\s+(.+?)"
        r"\s+(?:in|of|for)\s+(?:my\s+)?(.+)$", seg, re.IGNORECASE)
    if match:
        field = field_from_words(match.group(3))
        if field is None:
            return None
        return {"op": "replace", "field": field,
                "target": match.group(1).strip(" ."),
                "values": _values(match.group(2))}
    # remove/delete/drop X from <field>
    match = re.search(
        r"(?:remove|delete|drop|take out|take off)\s+(.+?)"
        r"\s+from\s+(?:my\s+)?(.+)$", seg, re.IGNORECASE)
    if match:
        field = field_from_words(match.group(2))
        if field is None:
            return None
        return {"op": "remove", "field": field,
                "values": _values(match.group(1)), "target": ""}
    # add X to <field>
    match = re.search(
        r"add\s+(.+?)\s+to\s+(?:my\s+)?(.+)$", seg, re.IGNORECASE)
    if match:
        field = field_from_words(match.group(2))
        if field is None:
            return None
        return {"op": "add", "field": field,
                "values": _values(match.group(1)), "target": ""}
    # clear/reset <field>
    match = re.search(r"(?:clear|reset|empty)\s+(?:my\s+)?(.+)$",
                      seg, re.IGNORECASE)
    if match and " to " not in lowered:
        field = field_from_words(match.group(1))
        if field is None:
            return None
        return {"op": "clear", "field": field, "values": [],
                "target": ""}
    # set/update/change <field> to X ; my <field> is X
    match = re.search(
        r"(?:set|update|change)\s+(?:my\s+)?(.+?)\s+to\s+(.+)$",
        seg, re.IGNORECASE)
    if match:
        field = field_from_words(match.group(1))
        if field is None:
            return None
        return {"op": "set", "field": field,
                "values": [match.group(2).strip(" .")], "target": ""}
    match = re.search(r"my\s+(.+?)\s+is\s+(.+)$", seg, re.IGNORECASE)
    if match:
        field = field_from_words(match.group(1))
        if field is None:
            return None
        return {"op": "set", "field": field,
                "values": [match.group(2).strip(" .")], "target": ""}
    match = re.search(r"^my\s+(.+?)\s+to\s+(.+)$", seg, re.IGNORECASE)
    if match:
        field = field_from_words(match.group(1))
        if field is None:
            return None
        return {"op": "set", "field": field,
                "values": [match.group(2).strip(" .")], "target": ""}
    return None


def _profile(twin) -> dict:
    try:
        data = json.loads(twin["profile_json"] or "{}")
    except (ValueError, TypeError):
        data = {}
    return dict(data) if isinstance(data, dict) else {}


def _authorized(twin) -> list:
    try:
        data = json.loads(twin["authorized_fields_json"] or "[]")
    except (ValueError, TypeError):
        data = []
    return list(data) if isinstance(data, list) else []


def snapshot(twin) -> dict:
    """Capture everything ``undo`` needs (JSON-safe)."""
    return {"display_name": twin["display_name"],
            "persona": twin["persona"], "tone": twin["tone"],
            "profile": _profile(twin),
            "authorized_fields": _authorized(twin)}


def _get_field(row: dict, profile: dict, field: str):
    kind = FIELDS[field][0]
    if kind == "twin":
        return row[field]
    value = profile.get(field)
    if kind == "list":
        return list(value) if isinstance(value, list) else []
    if kind == "number":
        return value if isinstance(value, int) else None
    return value if isinstance(value, str) else ""


def _short(value) -> str:
    if isinstance(value, list):
        return ", ".join(value) if value else "(empty)"
    return value if value else "(empty)"


def preview(conn, twin_id: int, ops: list[dict]) -> dict:
    """Describe what ops WOULD do, without writing. Pure read."""
    twin = get_twin(conn, twin_id)
    if twin is None:
        raise ValueError("Twin not found.")
    profile = _profile(twin)
    row = {"display_name": twin["display_name"],
           "persona": twin["persona"], "tone": twin["tone"]}
    applied: list[tuple] = []
    errors: list[str] = []

    def accept(item, change):
        applied.append((item, change))
        kind = FIELDS[item["field"]][0]
        if kind == "twin":
            row[item["field"]] = _apply_value(
                row[item["field"]], item, "text")
        else:
            profile[item["field"]] = _apply_value(
                profile.get(item["field"]), item, kind)

    for item in ops:
        field, kind = item["field"], FIELDS[item["field"]][0]
        current = _get_field(row, profile, field)
        if item["op"] == "add" and kind != "list":
            errors.append(f"Add works on lists; say 'set my {field}"
                          f" to ...' instead.")
            continue
        if item["op"] == "add":
            have = {v.lower() for v in current}
            new = [v for v in item["values"]
                   if v.lower() not in have]
            dupes = [v for v in item["values"]
                     if v.lower() in have]
            if not new:
                errors.append(f"{field} already has"
                              f" {', '.join(dupes) or 'that'}.")
                continue
            accept(item, {"field": field, "before": _short(current),
                            "after": _short([*current, *new])})
        elif item["op"] == "remove":
            if kind == "list":
                missing = [v for v in item["values"]
                           if v.lower() not in
                           {c.lower() for c in current}]
                if missing:
                    errors.append(f"{field} has no"
                                  f" {', '.join(missing)}.")
                    continue
                kept = [c for c in current if c.lower() not in
                        {v.lower() for v in item["values"]}]
                accept(item, {"field": field,
                                "before": _short(current),
                                "after": _short(kept)})
            else:
                gone, ok = _remove_text(current, item["values"])
                if not ok:
                    errors.append(f"I couldn't find"
                                  f" {', '.join(item['values'])} in"
                                  f" your {field}.")
                    continue
                accept(item, {"field": field,
                                "before": _short(current),
                                "after": _short(gone)})
        elif item["op"] == "replace":
            if kind == "list":
                if item["target"].lower() not in {
                        c.lower() for c in current}:
                    errors.append(f"{field} has no"
                                  f" {item['target']}.")
                    continue
                swapped = [
                    item["values"][0] if c.lower()
                    == item["target"].lower() else c
                    for c in current]
                accept(item, {"field": field,
                                "before": _short(current),
                                "after": _short(swapped)})
            else:
                done, ok = _replace_text(
                    current, item["target"], item["values"][0]
                    if item["values"] else "")
                if not ok:
                    errors.append(f"I couldn't find"
                                  f" {item['target']!r} in your"
                                  f" {field}.")
                    continue
                accept(item, {"field": field,
                                "before": _short(current),
                                "after": _short(done)})
        elif item["op"] == "set":
            value = item["values"][0] if item["values"] else ""
            if kind == "number":
                try:
                    value = int(value)
                    if not 0 <= value <= 80:
                        raise ValueError
                except (TypeError, ValueError):
                    errors.append("Experience years must be a whole number from 0 to 80.")
                    continue
            accept(item, {"field": field, "before": _short(current),
                            "after": _short(value)})
        elif item["op"] == "clear":
            accept(item, {"field": field, "before": _short(current),
                            "after": "(empty)", "destructive": True})
    changes = [change for _item, change in applied]
    destructive = any(c.get("destructive") for c in changes)
    return {"changes": changes, "errors": errors,
            "destructive": destructive, "applied": applied}


def _remove_text(current: str, values: list[str]):
    text = current or ""
    for value in values:
        at = text.lower().find(value.lower())
        if at < 0:
            return text, False
        text = (text[:at] + text[at + len(value):]).strip(" ;,")
    return " ".join(text.split()), True


def _replace_text(current: str, target: str, new: str):
    text = current or ""
    at = text.lower().find((target or "").lower())
    if at < 0 or not target:
        return text, False
    done = text[:at] + (new or "") + text[at + len(target):]
    return " ".join(done.split()), True


def apply(conn, user_id: int, twin_id: int,
          ops: list[dict], *, commit: bool = True) -> dict:
    """Apply ops to the OWNER's twin. Returns changes + undo snapshot."""
    twin = get_twin(conn, twin_id)
    if twin is None or twin["user_id"] != user_id:
        raise ValueError("You can only update your own twin.")
    report = preview(conn, twin_id, ops)
    if report["errors"]:
        return {"changes": [], "errors": report["errors"],
                "undone": None}
    if not report["changes"]:
        return {"changes": [], "errors": report["errors"],
                "undone": None}
    snap = snapshot(twin)
    profile = _profile(twin)
    row = {"display_name": twin["display_name"],
           "persona": twin["persona"], "tone": twin["tone"]}
    for item, _change in report["applied"]:
        field, kind = item["field"], FIELDS[item["field"]][0]
        if kind == "twin":
            row[field] = _apply_value(
                row[field], item, "text")
        else:
            profile[field] = _apply_value(
                profile.get(field), item, kind)
    update_twin(conn, twin_id, display_name=row["display_name"],
                persona=row["persona"], tone=row["tone"],
                profile=profile,
                authorized_fields=_authorized(twin))
    summary = "; ".join(f"{c['field']}: {c['before']} -> {c['after']}"
                        for c in report["changes"])[:400]
    audit(conn, None, "twin.updated", user_id,
          f"twin={twin_id} {summary}")
    if commit:
        conn.commit()
    return {"changes": report["changes"], "errors": report["errors"],
            "undone": snap}


def _apply_value(current, item: dict, kind: str):
    if item["op"] == "set":
        value = item["values"][0] if item["values"] else ""
        if kind == "number":
            return int(value)
        return _values(value) if kind == "list" else value[:1000]
    if item["op"] == "clear":
        return [] if kind == "list" else (None if kind == "number" else "")
    if kind == "list":
        items = list(current) if isinstance(current, list) else []
        if item["op"] == "add":
            have = {v.lower() for v in items}
            return items + [v[:120] for v in item["values"]
                            if v.lower() not in have][:30]
        if item["op"] == "remove":
            drop = {v.lower() for v in item["values"]}
            return [c for c in items if c.lower() not in drop]
        if item["op"] == "replace":
            return [item["values"][0][:120]
                    if c.lower() == item["target"].lower() else c
                    for c in items]
        return items
    text = current if isinstance(current, str) else ""
    if item["op"] == "remove":
        done, _ok = _remove_text(text, item["values"])
        return done
    if item["op"] == "replace":
        done, _ok = _replace_text(
            text, item["target"],
            item["values"][0] if item["values"] else "")
        return done
    return text


def scoped_snapshot(twin, fields: set[str]) -> dict:
    """Record only fields touched by one edit, for non-clobbering undo."""
    full = snapshot(twin)
    return {field: (full[field] if FIELDS[field][0] == "twin"
                    else full["profile"].get(field)) for field in fields}


def scoped_snapshot_from_full(snap: dict, fields: list[str]) -> dict:
    return {field: (snap[field] if FIELDS[field][0] == "twin"
                    else snap["profile"].get(field)) for field in fields}


def undo(conn, user_id: int, twin_id: int, action: dict,
         *, commit: bool = True) -> dict:
    """Undo just this edit; preserve fields changed independently later."""
    twin = get_twin(conn, twin_id)
    if twin is None or twin["user_id"] != user_id:
        raise ValueError("You can only update your own twin.")
    profile = _profile(twin)
    row = {key: twin[key] for key in ("display_name", "persona", "tone")}
    restored, conflicts = [], []
    before, after = action.get("before", {}), action.get("after", {})
    for field, expected in after.items():
        kind = FIELDS[field][0]
        current = row[field] if kind == "twin" else profile.get(field)
        if current != expected:
            conflicts.append(field)
            continue
        if kind == "twin":
            row[field] = before.get(field)
        else:
            profile[field] = before.get(field)
        restored.append(field)
    if restored:
        update_twin(conn, twin_id, display_name=row["display_name"],
                    persona=row["persona"], tone=row["tone"],
                    profile=profile, authorized_fields=_authorized(twin))
        audit(conn, None, "twin.update_undone", user_id,
              f"twin={twin_id} fields={','.join(restored)}")
        if commit:
            conn.commit()
    return {"restored": restored, "conflicts": conflicts}


def describe(report: dict) -> str:
    parts = []
    for change in report.get("changes", []):
        parts.append(f"{change['field']}: {change['before']} →"
                     f" {change['after']}")
    for error in report.get("errors", []):
        parts.append(error)
    if report.get("undone") is not None:
        parts.append("Say 'undo' to revert.")
    return " ".join(parts) if parts else "No changes."
