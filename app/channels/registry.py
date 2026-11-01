"""Channel registry: one entry per vendor + mock."""

from __future__ import annotations

from .base import Channel
from .meta_channels import InstagramChannel, MessengerChannel, WhatsAppChannel
from .mock_channel import MockChannel
from .telegram import TelegramChannel
from .types import ChannelError

CHANNELS: dict[str, Channel] = {
    "telegram": TelegramChannel(),
    "whatsapp": WhatsAppChannel(),
    "messenger": MessengerChannel(),
    "instagram": InstagramChannel(),
    "mock": MockChannel(),
}


def get_channel(name: str) -> Channel:
    channel = CHANNELS.get((name or "").lower())
    if channel is None:
        raise ChannelError(f"Unknown channel {name!r}.")
    return channel


def channel_names() -> list[str]:
    return sorted(CHANNELS)
