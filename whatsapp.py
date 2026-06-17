"""
whatsapp.py — Twilio WhatsApp send wrapper + inbound signature validation.

- send(to, body): send a WhatsApp message via Twilio.
- validate_signature(...): verify the X-Twilio-Signature on inbound webhooks
  using Twilio's RequestValidator against PUBLIC_WEBHOOK_URL (Section 9.2).
  No sandbox bypass — if the auth token isn't set, validation fails closed.

The Twilio client is built lazily so this module imports cleanly with no creds.
"""

from __future__ import annotations

import threading
from typing import Optional

import config
from logger import get_logger, redact_phone

log = get_logger("whatsapp")

_lock = threading.Lock()
_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is None:
            from twilio.rest import Client

            if not (config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN):
                raise RuntimeError("Twilio credentials not configured.")
            _client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
    return _client


def _normalize_to(to: str) -> str:
    """Ensure the destination has the 'whatsapp:' prefix Twilio expects."""
    to = str(to).strip()
    if not to.lower().startswith("whatsapp:"):
        to = "whatsapp:" + to
    return to


def send(to: str, body: str) -> Optional[str]:
    """Send a WhatsApp message. Returns the message SID, or None on failure.

    Failures are logged but never raised — a failed receipt must not break the
    webhook flow (both webhooks must return 200 fast, Section 9.8).
    """
    if not to:
        log.warning("send() called with no destination; skipping.")
        return None
    try:
        client = _get_client()
        msg = client.messages.create(
            from_=config.TWILIO_WHATSAPP_FROM,
            to=_normalize_to(to),
            body=body,
        )
        log.info("Sent WhatsApp to %s sid=%s", redact_phone(to), msg.sid)
        return msg.sid
    except Exception as exc:  # noqa: BLE001 - fail safe, never crash sender
        log.error("Failed to send WhatsApp to %s: %s", redact_phone(to), exc)
        return None


def validate_signature(signature: Optional[str], params: dict, url: Optional[str] = None) -> bool:
    """Validate an inbound Twilio webhook signature.

    Uses RequestValidator against PUBLIC_WEBHOOK_URL (not request.url) so it works
    behind tunnels/proxies. Fails closed if the auth token isn't configured.
    """
    if not config.TWILIO_AUTH_TOKEN:
        log.error("TWILIO_AUTH_TOKEN not set — cannot validate, rejecting.")
        return False
    if not signature:
        log.warning("Inbound WhatsApp missing X-Twilio-Signature.")
        return False
    try:
        from twilio.request_validator import RequestValidator

        validator = RequestValidator(config.TWILIO_AUTH_TOKEN)
        check_url = url or config.PUBLIC_WEBHOOK_URL
        ok = validator.validate(check_url, params, signature)
        if not ok:
            log.warning("Twilio signature validation failed.")
        return ok
    except Exception as exc:  # noqa: BLE001
        log.error("Error validating Twilio signature: %s", exc)
        return False
