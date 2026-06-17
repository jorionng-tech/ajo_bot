"""
logger.py — Rotating file logger with PII redaction.

Security (Section 9.7):
- Phone numbers are redacted to the form +234***890.
- Member names are never logged; trace activity by Paystack reference instead.
- Secrets (Paystack key, Twilio token, Google credentials) must never be passed
  to the logger. Helpers here only redact phones; do not log secret values.

This module has NO external dependencies and is safe to import anywhere.
"""

from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
LOG_DIR = os.environ.get("AJO_LOG_DIR", "logs")
LOG_FILE = os.path.join(LOG_DIR, "ajo.log")
LOG_LEVEL = os.environ.get("AJO_LOG_LEVEL", "INFO").upper()
MAX_BYTES = 2 * 1024 * 1024  # 2 MB per file
BACKUP_COUNT = 5

# Matches +234XXXXXXXXXX style numbers (optionally with whatsapp: prefix).
_PHONE_RE = re.compile(r"(\+?\d{7,15})")


def redact_phone(phone: str | None) -> str:
    """Redact a phone number to +234***890 form.

    Keeps a short prefix and the last 3 digits; masks the middle. Safe to call
    with None or non-string input.
    """
    if not phone:
        return "<none>"
    s = str(phone).strip()
    # Drop a leading "whatsapp:" prefix if present.
    if s.lower().startswith("whatsapp:"):
        s = s.split(":", 1)[1]
    digits = re.sub(r"\D", "", s)
    if len(digits) < 7:
        return "***"
    prefix = digits[:3]
    suffix = digits[-3:]
    sign = "+" if s.startswith("+") else ""
    return f"{sign}{prefix}***{suffix}"


def redact_text(text: str | None) -> str:
    """Redact any phone-number-looking substrings inside a free-text string."""
    if not text:
        return ""
    return _PHONE_RE.sub(lambda m: redact_phone(m.group(1)), str(text))


class _RedactionFilter(logging.Filter):
    """Logging filter that redacts phone numbers in the final message."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            redacted = redact_text(msg)
            if redacted != msg:
                record.msg = redacted
                record.args = ()
        except Exception:
            # Never let logging break the app.
            pass
        return True


_configured = False


def _configure_root() -> None:
    global _configured
    if _configured:
        return

    os.makedirs(LOG_DIR, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(_RedactionFilter())

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(_RedactionFilter())

    root = logging.getLogger("ajo")
    root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    # Avoid duplicate handlers if re-imported.
    if not root.handlers:
        root.addHandler(file_handler)
        root.addHandler(console_handler)
    root.propagate = False

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger under the 'ajo' root, with redaction enabled."""
    _configure_root()
    return logging.getLogger(f"ajo.{name}")
