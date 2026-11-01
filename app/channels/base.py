"""Channel interface + shared JSON transport (stdlib urllib)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .types import (
    ChannelError,
    ChannelNotConfigured,
    DeliveryReceipt,
    IncomingMessage,
)

TIMEOUT_SECONDS = 30


def http_post_json(url: str, payload: dict, headers: dict,
                   timeout: int = TIMEOUT_SECONDS) -> dict:
    """POST JSON, parse the JSON reply. Failures become ChannelError."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except OSError:
            detail = ""
        raise ChannelError(
            f"Vendor HTTP {exc.code}: {detail}".rstrip()) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ChannelError(f"Vendor request failed: {exc}.") from exc
    except ValueError as exc:
        raise ChannelError(
            f"Vendor reply was not valid JSON: {exc}.") from exc


class Channel:
    """One messaging vendor behind the unified surface."""

    name = "base"
    label = "Base"

    def setup_requirements(self) -> str:
        """Human-readable account setup the vendor needs."""
        return ""

    def verify_webhook(self, secret: str, body: bytes,
                       headers: dict) -> bool:
        """True when the webhook genuinely came from the vendor."""
        raise NotImplementedError

    def parse_webhook(self, body: bytes) -> list[IncomingMessage]:
        """Extract inbound messages (never raises on unknown shapes)."""
        raise NotImplementedError

    def send(self, post, credentials: dict, to_external_id: str,
             body: str, idempotency_key: str) -> DeliveryReceipt:
        """Send one message. ``post`` is the injectable transport."""
        raise NotImplementedError
