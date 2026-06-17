"""
emailer.py — Optional Brevo (Sendinblue) transactional email receipts.

Only used when ajo_config.json has "send_email_receipt": true AND Brevo creds are
present. Sends a simple contribution receipt. Like the other senders, it fails
safe: errors are logged, never raised.
"""

from __future__ import annotations

from typing import Optional

import config
from logger import get_logger

log = get_logger("emailer")

_API = "https://api.brevo.com/v3/smtp/email"


def is_enabled() -> bool:
    """True only when email receipts are turned on and credentials are present."""
    return bool(
        config.SEND_EMAIL_RECEIPT
        and config.BREVO_API_KEY
        and config.BREVO_SENDER_EMAIL
    )


def send_receipt(
    to_email: str,
    to_name: str,
    subject: str,
    text_body: str,
    html_body: Optional[str] = None,
) -> bool:
    """Send a transactional email via Brevo. Returns True on success.

    No-ops (returns False) when email receipts are disabled or misconfigured.
    """
    if not is_enabled():
        log.info("Email receipts disabled or Brevo not configured — skipping.")
        return False
    if not to_email:
        log.warning("send_receipt called with no recipient — skipping.")
        return False
    try:
        import requests

        sender = {"email": config.BREVO_SENDER_EMAIL}
        if config.BREVO_SENDER_NAME:
            sender["name"] = config.BREVO_SENDER_NAME

        payload = {
            "sender": sender,
            "to": [{"email": to_email, "name": to_name or to_email}],
            "subject": subject,
            "textContent": text_body,
        }
        if html_body:
            payload["htmlContent"] = html_body

        resp = requests.post(
            _API,
            headers={
                "api-key": config.BREVO_API_KEY,
                "Content-Type": "application/json",
                "accept": "application/json",
            },
            json=payload,
            timeout=10,
        )
        if resp.status_code not in (200, 201):
            log.error("Brevo send failed: %s %s", resp.status_code, resp.text[:200])
            return False
        log.info("Sent email receipt to %s", to_email)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("Brevo email error: %s", exc)
        return False
