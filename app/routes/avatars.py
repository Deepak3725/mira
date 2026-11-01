"""Avatar image endpoint (SVG served for reuse in later phases)."""

from __future__ import annotations

from fastapi import APIRouter, Response

from ..avatars import STATES, avatar_svg

router = APIRouter(tags=["avatars"])


@router.get("/avatar/{seed}.svg")
def avatar(seed: str, state: str = "idle") -> Response:
    """Return a twin mascot as SVG. Unknown states fall back to idle."""
    safe_seed = seed[:64]
    safe_state = state if state in STATES else "idle"
    return Response(content=avatar_svg(safe_seed, safe_state), media_type="image/svg+xml")
