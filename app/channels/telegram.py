"""Telegram channel (Bot API, stdlib urllib).

Setup: the owner creates a bot with @BotFather (free, no review)
and pastes the bot token when connecting; the token stays
server-side in channel_accounts.credentials_json, never rendered.
Sends go to POST https://api.telegram.org/bot<token>/sendMessage
with chat_id/text; a send counts only when the API answers ok:true
with a message_id (kept as the delivery receipt).
Webhooks verify with the X-Telegram-Bot-Api-Secret-Token header.
No token is configured here, so live sends are NOT yet verified:
tests use fake transports only.
"""

from __future__ import annotations

import hmac
import json

from .base import Channel, http_post_json
from .types import (
    ChannelError,
    ChannelNotConfigured,
    DeliveryReceipt,
    IncomingMessage,
)


class TelegramChannel(Channel):
    name = "telegram"
    label = "Telegram"

    def setup_requirements(self) -> str:
        return ("Create a bot with @BotFather and paste its token. "
                "Each contact needs its numeric chat id (message the "
                "bot once, then read getUpdates).")

    def verify_webhook(self, secret: str, body: bytes,
                       headers: dict) -> bool:
        if not secret:
            return False
        lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
        presented = lowered.get("x-telegram-bot-api-secret-token", "")
        return hmac.compare_digest(str(presented), secret)

    def parse_webhook(self, body: bytes) -> list[IncomingMessage]:
        try:
            data = json.loads((body or b"").decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return []
        if not isinstance(data, dict):
            return []
        message = data.get("message") or data.get("edited_message")
        if not isinstance(message, dict):
            return []
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return []
        name = " ".join(str(sender.get(part) or "")
                        for part in ("first_name", "last_name")).strip()
        return [IncomingMessage(
            external_id=str(chat.get("id", "")),
            sender_name=name or str(sender.get("username", "")),
            body=text.strip(),
            sent_at=str(message.get("date", "")),
            provider_msg_id=str(message.get("message_id", "")),
            raw={"update_id": data.get("update_id")})]

    def send(self, post, credentials: dict, to_external_id: str,
             body: str, idempotency_key: str) -> DeliveryReceipt:
        token = (credentials or {}).get("bot_token", "")
        if not token:
            raise ChannelNotConfigured(
                "Telegram needs a bot token (see setup).")
        if not (to_external_id or "").strip():
            raise ChannelError("Telegram needs a chat id recipient.")
        text = (body or "").strip()
        if not text:
            raise ChannelError("Telegram messages cannot be empty.")
        data = (post or http_post_json)(
            f"https://api.telegram.org/bot{token}/sendMessage",
            {"chat_id": to_external_id.strip(), "text": text[:4000]},
            {"Content-Type": "application/json"})
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise ChannelError(
                f"Telegram refused the send: {data!r}"[:200])
        message = data.get("result") or {}
        receipt = str(message.get("message_id", ""))
        if not receipt:
            raise ChannelError("Telegram sent but gave no message id.")
        return DeliveryReceipt(provider_msg_id=receipt, status="sent")
