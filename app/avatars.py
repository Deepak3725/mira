"""Original programmatic SVG mascots for MIRA twins.

Each twin gets a cute, deterministic companion generated from its avatar
seed. No image APIs and no copied characters: simple geometric shapes
(round body, big eyes, blush, ear variants) in distinct palettes.

Visual states (idle / thinking / communicating) are expressed through the
``state-*`` CSS class plus small SVG variations (mouth shape, thought dots,
chat waves). Motion lives in ``static/css/style.css``.
"""

from __future__ import annotations

import hashlib
import random
from xml.sax.saxutils import escape

STATES = ("idle", "thinking", "communicating")

PALETTES = (
    {"body": "#5eead4", "belly": "#ccfbf1", "accent": "#0f766e", "cheek": "#f9a8d4", "ear": "#14b8a6"},
    {"body": "#fcd34d", "belly": "#fef3c7", "accent": "#b45309", "cheek": "#fda4af", "ear": "#f59e0b"},
    {"body": "#c4b5fd", "belly": "#ede9fe", "accent": "#6d28d9", "cheek": "#f9a8d4", "ear": "#8b5cf6"},
    {"body": "#93c5fd", "belly": "#dbeafe", "accent": "#1d4ed8", "cheek": "#fda4af", "ear": "#3b82f6"},
    {"body": "#f9a8d4", "belly": "#fce7f3", "accent": "#9d174d", "cheek": "#fb7185", "ear": "#ec4899"},
    {"body": "#86efac", "belly": "#dcfce7", "accent": "#166534", "cheek": "#fda4af", "ear": "#22c55e"},
)

EAR_STYLES = ("round", "pointy", "antenna", "tuft")


def _rng_for(seed: str) -> random.Random:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _ears(style: str, ear: str, belly: str) -> str:
    if style == "round":
        return (
            f'<circle cx="33" cy="42" r="11" fill="{ear}"/>'
            f'<circle cx="87" cy="42" r="11" fill="{ear}"/>'
            f'<circle cx="33" cy="42" r="5" fill="{belly}"/>'
            f'<circle cx="87" cy="42" r="5" fill="{belly}"/>'
        )
    if style == "pointy":
        return (
            f'<polygon points="26,48 38,18 50,44" fill="{ear}"/>'
            f'<polygon points="94,48 82,18 70,44" fill="{ear}"/>'
        )
    if style == "antenna":
        return (
            f'<line x1="60" y1="38" x2="60" y2="18" stroke="{ear}" stroke-width="4" stroke-linecap="round"/>'
            f'<circle cx="60" cy="14" r="7" fill="{ear}"/>'
            f'<circle cx="58" cy="12" r="2" fill="#ffffff" opacity="0.8"/>'
        )
    # tuft
    return (
        f'<ellipse cx="50" cy="34" rx="6" ry="10" fill="{ear}" transform="rotate(-20 50 34)"/>'
        f'<ellipse cx="60" cy="30" rx="6" ry="11" fill="{ear}"/>'
        f'<ellipse cx="70" cy="34" rx="6" ry="10" fill="{ear}" transform="rotate(20 70 34)"/>'
    )


def _mouth(state: str, accent: str) -> str:
    if state == "thinking":
        return f'<line x1="54" y1="84" x2="66" y2="84" stroke="{accent}" stroke-width="3" stroke-linecap="round"/>'
    if state == "communicating":
        return f'<ellipse cx="60" cy="84" rx="5" ry="6" fill="{accent}"/>'
    return (
        f'<path d="M51,80 Q60,88 69,80" fill="none" stroke="{accent}" '
        'stroke-width="3" stroke-linecap="round"/>'
    )


def avatar_svg(seed: str, state: str = "idle") -> str:
    """Render the mascot SVG for ``seed`` in the given visual ``state``."""
    if state not in STATES:
        raise ValueError(f"unknown avatar state: {state!r}")
    rng = _rng_for(seed)
    pal = PALETTES[rng.randrange(len(PALETTES))]
    ears = _ears(EAR_STYLES[rng.randrange(len(EAR_STYLES))], pal["ear"], pal["belly"])
    pupil_dy = -3 if state == "thinking" else 1
    label = escape(f"MIRA companion {seed}")
    return (
        f'<svg class="mira-avatar state-{state}" viewBox="0 0 120 120" role="img" '
        f'aria-label="{label}" data-seed="{escape(seed)}">'
        '<g class="bob">'
        f"{ears}"
        f'<ellipse cx="46" cy="100" rx="9" ry="5" fill="{pal["accent"]}" opacity="0.45"/>'
        f'<ellipse cx="74" cy="100" rx="9" ry="5" fill="{pal["accent"]}" opacity="0.45"/>'
        f'<ellipse cx="60" cy="68" rx="35" ry="32" fill="{pal["body"]}"/>'
        f'<ellipse cx="60" cy="86" rx="20" ry="14" fill="{pal["belly"]}" opacity="0.9"/>'
        f'<ellipse cx="46" cy="64" rx="9" ry="11" fill="#ffffff"/>'
        f'<ellipse cx="74" cy="64" rx="9" ry="11" fill="#ffffff"/>'
        f'<circle cx="46" cy="{64 + pupil_dy}" r="4.5" fill="#1c2422"/>'
        f'<circle cx="74" cy="{64 + pupil_dy}" r="4.5" fill="#1c2422"/>'
        f'<circle cx="47.5" cy="{62 + pupil_dy}" r="1.5" fill="#ffffff"/>'
        f'<circle cx="75.5" cy="{62 + pupil_dy}" r="1.5" fill="#ffffff"/>'
        f'<ellipse cx="35" cy="76" rx="6" ry="4" fill="{pal["cheek"]}" opacity="0.85"/>'
        f'<ellipse cx="85" cy="76" rx="6" ry="4" fill="{pal["cheek"]}" opacity="0.85"/>'
        f"{_mouth(state, pal['accent'])}"
        "</g>"
        '<g class="think-dots" opacity="0">'
        f'<circle cx="94" cy="32" r="3" fill="{pal["accent"]}"/>'
        f'<circle cx="102" cy="24" r="2.2" fill="{pal["accent"]}"/>'
        f'<circle cx="109" cy="17" r="1.6" fill="{pal["accent"]}"/>'
        "</g>"
        '<g class="chat-waves" opacity="0" fill="none" '
        f'stroke="{pal["accent"]}" stroke-width="2.5" stroke-linecap="round">'
        '<path d="M97,62 q7,7 0,14"/>'
        '<path d="M104,57 q11,11 0,24"/>'
        "</g>"
        "</svg>"
    )
