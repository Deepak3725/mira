"""Timezone-aware scheduling + unambiguous recipient resolution.

Times like "11 PM" resolve in the owner's timezone to a UTC stamp. A
time that already passed today means tomorrow (documented, tested).
Recipients resolve by exact-then-substring match; zero or multiple
matches refuse -- MIRA never sends to an ambiguous recipient.
"""

from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .types import ChannelError

_TIME = re.compile(
    r"^\s*(\d{1,2})(?::(\d{2}))?\s*([AaPp])\.?\s*[Mm]\.?\s*$|"
    r"^\s*(\d{1,2}):(\d{2})\s*$")
_ISO = re.compile(r"^\s*\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?\s*$")


def _tzinfo(tz_name: str):
    name = (tz_name or "").strip() or "UTC"
    if name.upper() == "UTC":
        return dt.timezone.utc
    offset = re.fullmatch(r"UTC([+-])(\d{1,2})(?::?(\d{2}))?",
                          name, re.IGNORECASE)
    if offset:
        sign = 1 if offset.group(1) == "+" else -1
        minutes = sign * (int(offset.group(2)) * 60
                          + int(offset.group(3) or 0))
        return dt.timezone(dt.timedelta(minutes=minutes),
                           name.upper())
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ChannelError(
            f"Unknown timezone {name!r}. Use an IANA name (e.g. "
            "Europe/London), UTC, or a UTC offset like UTC+5.") from exc


def parse_time_to_utc(time_str: str, tz_name: str,
                      now_utc: dt.datetime | None = None) -> str:
    """Resolve a local time to a UTC 'YYYY-MM-DD HH:MM:SS' stamp."""
    text = (time_str or "").strip()
    if not text:
        raise ChannelError("Scheduled messages need a time.")
    tz = _tzinfo(tz_name)
    now = now_utc or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    local_now = now.astimezone(tz)
    iso = _ISO.match(text)
    if iso:
        day = text.strip()[:10]
        clock = text.strip()[11:16]
        try:
            naive = dt.datetime.strptime(f"{day} {clock}", "%Y-%m-%d %H:%M")
        except ValueError as exc:
            raise ChannelError(f"Bad date/time {text!r}.") from exc
        moment = naive.replace(tzinfo=tz)
    else:
        match = _TIME.match(text)
        if not match:
            raise ChannelError(
                f"Bad time {text!r}: use '11 PM', '23:00', or a full date.")
        if match.group(4) is not None:
            hour, minute = int(match.group(4)), int(match.group(5))
        else:
            hour, minute = int(match.group(1)), int(match.group(2) or 0)
            meridiem = match.group(3).upper()
            if meridiem == "P" and hour != 12:
                hour += 12
            if meridiem == "A" and hour == 12:
                hour = 0
        if hour > 23 or minute > 59:
            raise ChannelError(f"Bad time {text!r}.")
        moment = local_now.replace(hour=hour, minute=minute, second=0,
                                   microsecond=0)
        if moment <= local_now:
            moment += dt.timedelta(days=1)
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def resolve_recipient(contacts: list, name: str) -> dict:
    """Resolve a display name to exactly one contact or refuse."""
    want = (name or "").strip().lower()
    if not want:
        raise ChannelError("Scheduled messages need a recipient.")
    exact = [c for c in contacts
             if str(c["display_name"]).strip().lower() == want]
    if len(exact) == 1:
        return dict(exact[0])
    pool = exact or [c for c in contacts
                     if want in str(c["display_name"]).strip().lower()]
    if not pool:
        raise ChannelError(f"No contact matches {name!r}.")
    if len(pool) > 1:
        names = ", ".join(sorted(str(c["display_name"]) for c in pool))
        raise ChannelError(
            f"Recipient {name!r} is ambiguous ({names}); be specific.")
    return dict(pool[0])
