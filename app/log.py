"""Structured redacting logs (standard library only).

Secrets must never reach logs: `redact` scrubs credential-bearing values
before they are emitted, and the JSON formatter applies it to every
record. Request bodies, headers, and cookies are never logged.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re

REDACTED = "[REDACTED]"

_SENSITIVE_KEYS = (
    "password", "passwd", "secret", "api_key", "apikey", "token",
    "csrf", "session", "authorization", "cookie", "set-cookie", "key",
)

_CRED_URL = re.compile(r"(https?://[^/\s:@?#]+):[^/\s@?#]+@")

_STD_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message",
})


def _scrub_text(text: str) -> str:
    return _CRED_URL.sub(r"\1:" + REDACTED + "@", text)


def redact(value):
    """Recursively replace secret-bearing values with [REDACTED]."""
    if isinstance(value, dict):
        return {
            key: (REDACTED if str(key).lower() in _SENSITIVE_KEYS
                  else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _scrub_text(value)
    return value


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, message, redacted extras."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": dt.datetime.fromtimestamp(
                record.created, dt.timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, item in record.__dict__.items():
            if key not in _STD_ATTRS:
                payload[key] = redact(item)
        return json.dumps(payload, default=str)


def get_logger(name: str = "mira") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


_configured = False


def configure_stderr(level: int = logging.INFO) -> None:
    """Attach a JSON stderr handler (once). Called at app startup."""
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger = get_logger()
    logger.addHandler(handler)
    logger.setLevel(level)
    _configured = True
