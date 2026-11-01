"""WhatsApp / Messenger / Instagram channels (Meta platform).

Investigation result (no paid services here): all three need a Meta
developer app + review and owner-side assets (a phone-number id and
approved templates for WhatsApp; a Page + Page token for Messenger;
a linked Instagram business account for Instagram messaging). None
of that exists in this project, so sends honestly refuse with the
exact setup each vendor needs. Webhook signature verification
(X-Hub-Signature-256 HMAC) IS implemented and tested, as is
defensive inbound parsing, so the day credentials exist only the
send call is missing -- and no message is ever claimed sent.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from .base import Channel
from .types import ChannelNotConfigured, IncomingMessage


def verify_meta_signature(app_secret: str, body: bytes,
                          headers: dict) -> bool:
    """Validate X-Hub-Signature-256 (sha256=<hex of raw body>)."""
    if not app_secret:
        return False
    lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
    presented = str(lowered.get("x-hub-signature-256", ""))
    if not presented.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), body or b"",
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(presented[7:], expected)


def _whatsapp_messages(data: dict) -> list[IncomingMessage]:
    out = []
    if not isinstance(data, dict):
        return out
    for entry in data.get("entry", []) or []:
        for change in (entry or {}).get("changes", []) or []:
            value = (change or {}).get("value", {}) or {}
            for message in value.get("messages", []) or []:
                if not isinstance(message, dict):
                    continue
                if message.get("type") != "text":
                    continue
                text = ((message.get("text") or {}).get("body") or "")
                if not isinstance(text, str) or not text.strip():
                    continue
                out.append(IncomingMessage(
                    external_id=str(message.get("from", "")),
                    sender_name="",
                    body=text.strip(),
                    sent_at=str(message.get("timestamp", "")),
                    provider_msg_id=str(message.get("id", ""))))
    return out


def _messenger_messages(data: dict) -> list[IncomingMessage]:
    out = []
    if not isinstance(data, dict):
        return out
    for entry in data.get("entry", []) or []:
        for event in (entry or {}).get("messaging", []) or []:
            if not isinstance(event, dict):
                continue
            message = event.get("message") or {}
            text = message.get("text", "")
            if not isinstance(text, str) or not text.strip():
                continue
            sender = event.get("sender") or {}
            out.append(IncomingMessage(
                external_id=str(sender.get("id", "")),
                sender_name="",
                body=text.strip(),
                sent_at=str(message.get("mid", "")),
                provider_msg_id=str(message.get("mid", ""))))
    return out


class _MetaChannel(Channel):
    app_name = "Meta"

    def verify_webhook(self, secret: str, body: bytes,
                       headers: dict) -> bool:
        return verify_meta_signature(secret, body, headers)


class WhatsAppChannel(_MetaChannel):
    name = "whatsapp"
    label = "WhatsApp"

    def setup_requirements(self) -> str:
        return ("Meta developer app + a WhatsApp phone-number id, a "
                "permanent access token, and approved message "
                "templates (business-initiated texts need templates).")

    def parse_webhook(self, body: bytes) -> list[IncomingMessage]:
        try:
            return _whatsapp_messages(
                json.loads((body or b"").decode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            return []

    def send(self, post, credentials: dict, to_external_id: str,
             body: str, idempotency_key: str):
        raise ChannelNotConfigured(
            "WhatsApp send needs META app credentials + phone-number "
            "id + approved template (see setup). Nothing was sent.")


class MessengerChannel(_MetaChannel):
    name = "messenger"
    label = "Messenger"

    def setup_requirements(self) -> str:
        return ("Meta developer app + a Facebook Page with a Page "
                "access token, subscribed to messaging events.")

    def parse_webhook(self, body: bytes) -> list[IncomingMessage]:
        try:
            return _messenger_messages(
                json.loads((body or b"").decode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            return []

    def send(self, post, credentials: dict, to_external_id: str,
             body: str, idempotency_key: str):
        raise ChannelNotConfigured(
            "Messenger send needs a Page access token (see setup). "
            "Nothing was sent.")


class InstagramChannel(_MetaChannel):
    name = "instagram"
    label = "Instagram"

    def setup_requirements(self) -> str:
        return ("Meta developer app + an Instagram business account "
                "linked to a Facebook Page, with messaging enabled.")

    def parse_webhook(self, body: bytes) -> list[IncomingMessage]:
        try:
            return _messenger_messages(
                json.loads((body or b"").decode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            return []

    def send(self, post, credentials: dict, to_external_id: str,
             body: str, idempotency_key: str):
        raise ChannelNotConfigured(
            "Instagram send needs a linked business account + token "
            "(see setup). Nothing was sent.")
