"""Logging filter that strips credentials and tokens from log records."""

import json
import logging
import re
from datetime import datetime, timezone

_PATTERNS = [
    (
        re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?)(bearer\s+)?[^\s\"',}]+"),
        r"\1[REDACTED]",
    ),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-_\.=]+"), "Bearer [REDACTED]"),
    (
        re.compile(
            r"(?i)([\"']?(?:password|access|refresh|token|secret|card_number|cvv)[\"']?"
            r"\s*[:=]\s*[\"']?)[^\s\"',}]+"
        ),
        r"\1[REDACTED]",
    ),
    # Bare JWTs (three base64url segments)
    (re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"), "[REDACTED_JWT]"),
]


def redact(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RequestIdFilter(logging.Filter):
    """Adds `request_id` (the correlation id of the current request / message)."""

    def filter(self, record: logging.LogRecord) -> bool:
        from .context import get_request_id

        if not hasattr(record, "request_id"):
            record.request_id = get_request_id()
        return True


class RedactSensitiveDataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never break logging
            return True
        redacted = redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


_STANDARD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line; `extra={...}` fields become top-level keys."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, default=str))


class KeyValueFormatter(logging.Formatter):
    """Human-readable dev format that still shows structured `extra` fields."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _STANDARD_ATTRS and not k.startswith("_")
        }
        if extras:
            base += " " + " ".join(f"{k}={v}" for k, v in extras.items())
        return redact(base)
