"""Mock channel for tests, demos, and the no-vendor default.

Sends return clearly-labelled fake receipts (mock-*) and never touch
a network. Webhook verification accepts only the literal test secret.
"""

from __future__ import annotations

import hmac
import json
import uuid

from .base import Channel
from .types import DeliveryReceipt, IncomingMessage


class MockChannel(Channel):
    name = "mock"
    label = "Mock"

    def setup_requirements(self) -> str:
        return "None. The mock channel always works and sends nowhere."

    def verify_webhook(self, secret: str, body: bytes,
                       headers: dict) -> bool:
        lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
        presented = str(lowered.get("x-mock-secret", ""))
        return bool(secret) and hmac.compare_digest(presented, secret)

    def parse_webhook(self, body: bytes) -> list[IncomingMessage]:
        try:
            data = json.loads((body or b"").decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return []
        if not isinstance(data, dict) or not isinstance(
                data.get("text"), str) or not data["text"].strip():
            return []
        return [IncomingMessage(
            external_id=str(data.get("from", "mock-peer")),
            sender_name=str(data.get("name", "")),
            body=data["text"].strip(),
            provider_msg_id=str(data.get("id", "")))]

    def send(self, post, credentials: dict, to_external_id: str,
             body: str, idempotency_key: str) -> DeliveryReceipt:
        text = (body or "").strip()
        if not text:
            from .types import ChannelError

            raise ChannelError("Mock messages cannot be empty.")
        return DeliveryReceipt(
            provider_msg_id=f"mock-{idempotency_key or uuid.uuid4().hex[:8]}",
            status="sent")
