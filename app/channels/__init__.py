"""External messaging framework (Phase 4): channels, outbox, queue."""

from .base import Channel, http_post_json
from .meta_channels import (
    InstagramChannel,
    MessengerChannel,
    WhatsAppChannel,
    verify_meta_signature,
)
from .mock_channel import MockChannel
from .outbox import (
    ACCOUNT_STATUSES,
    CHANNEL_NAMES,
    MESSAGE_STATUSES,
    activate_account,
    add_contact,
    cancel_message,
    create_account,
    draft_message,
    get_account,
    get_message,
    list_accounts,
    list_contacts,
    log_channel_event,
    log_inbound,
    process_due,
    retry_message,
    revoke_account,
    schedule_message,
    send_now,
    set_permissions,
)
from .registry import CHANNELS, channel_names, get_channel
from .scheduler import parse_time_to_utc, resolve_recipient
from .telegram import TelegramChannel
from .types import (
    ChannelError,
    ChannelNotConfigured,
    DeliveryReceipt,
    IncomingMessage,
)

__all__ = [
    "ACCOUNT_STATUSES",
    "CHANNELS",
    "CHANNEL_NAMES",
    "MESSAGE_STATUSES",
    "Channel",
    "ChannelError",
    "ChannelNotConfigured",
    "DeliveryReceipt",
    "IncomingMessage",
    "InstagramChannel",
    "MessengerChannel",
    "MockChannel",
    "TelegramChannel",
    "WhatsAppChannel",
    "activate_account",
    "add_contact",
    "cancel_message",
    "channel_names",
    "create_account",
    "draft_message",
    "get_account",
    "get_channel",
    "get_message",
    "http_post_json",
    "list_accounts",
    "list_contacts",
    "log_channel_event",
    "log_inbound",
    "parse_time_to_utc",
    "process_due",
    "resolve_recipient",
    "retry_message",
    "revoke_account",
    "schedule_message",
    "send_now",
    "set_permissions",
    "verify_meta_signature",
]
