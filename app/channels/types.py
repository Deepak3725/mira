"""Shared types for external messaging."""

from __future__ import annotations

from dataclasses import dataclass, field


class ChannelError(Exception):
    """User-facing channel failure (send refused, webhook invalid...)."""


class ChannelNotConfigured(ChannelError):
    """Account lacks the credentials/setup the vendor requires."""


@dataclass(frozen=True)
class IncomingMessage:
    """One inbound message parsed from a webhook payload."""

    external_id: str
    sender_name: str
    body: str
    sent_at: str = ""
    provider_msg_id: str = ""
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryReceipt:
    """Proof a send happened. Never fabricate one."""

    provider_msg_id: str
    status: str = "sent"
