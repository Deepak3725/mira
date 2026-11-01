"""Privacy helpers: allowlist-based field filtering.

Rule: NOTHING is shared unless explicitly listed in the twin's
``authorized_fields``. Filtering happens server-side, before any prompt is
built or any public view is rendered. Frontend hiding is not a privacy
mechanism.
"""

from __future__ import annotations

import json
from sqlite3 import Row
from typing import Any


def authorized_view(
    profile: dict[str, Any], authorized_fields: list | None
) -> dict[str, Any]:
    """Return a new dict with ONLY explicitly authorized top-level fields.

    Unknown or non-string entries in ``authorized_fields`` are ignored;
    missing keys are skipped. The input profile is never mutated.
    """
    if not isinstance(profile, dict):
        return {}
    allowed = {f for f in (authorized_fields or []) if isinstance(f, str)}
    return {key: profile[key] for key in allowed if key in profile}


def _parse_json_dict(raw: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_json_list(raw: Any) -> list:
    try:
        parsed = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def twin_public_view(twin: Row) -> dict[str, Any]:
    """Public (shareable) view of a twin row from ``list_twins``/``get``."""
    profile = _parse_json_dict(twin["profile_json"])
    allowed = _parse_json_list(twin["authorized_fields_json"])
    return authorized_view(profile, allowed)
