"""Deterministic knowledge classifier (no model calls).

First matching rule wins; anything else is 'general'. Rules are
deliberately conservative: a wrong category only mis-files a pending
item the owner reviews anyway, but tests pin the behaviour.
"""

from __future__ import annotations

import re

_RULES: tuple[tuple[str, str], ...] = (
    ("preference",
     r"\bprefer\b|i (love|like|hate|dislike)|favo[u]?rite|please (always|never)"
     r"|don't call me|my pronouns"),
    ("skill",
     r"\bi know how\b|\bi can (build|write|speak|play)\b|proficient|certified"
     r"|certification|fluent in|my stack is"),
    ("experience",
     r"\bworked at\b|\bworked on\b|years of |previously |former |\bmy background\b"
     r"|at my (last|previous|current) job|in my role as"),
    ("project",
     r"\bproject\b|\bbuilt\b|\bbuilding\b|side project|\bstartup\b|\brepo\b"
     r"|my (thesis|app|website|bot)"),
    ("interest",
     r"interested in|\bhobb\w+|\benjoy\b|passionate|\bfan of\b|\bi read\b"
     r"|\bi watch\b|\bi play\b"),
    ("style",
     r"keep it short|\bbe brief\b|explain like|step by step|in detail"
     r"|\bbullet points\b|no emoji|be (direct|concise|formal|casual)"),
)

_COMPILED = [(category, re.compile(pattern, re.IGNORECASE))
             for category, pattern in _RULES]


def classify(text: str) -> str:
    """Assign one knowledge category to a candidate's content."""
    lowered = text or ""
    for category, pattern in _COMPILED:
        if pattern.search(lowered):
            return category
    return "general"
