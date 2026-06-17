"""
notifier.py — Telegram admin notifications (Section 0.6: fail safe, fail loud).

Sends short operational messages to the secretary/admin via the Telegram Bot API.
Used for: new pending member, payment confirmed, payment without prior JOIN,
reactivations, monthly reminder summaries, and error alerts.

Failures are logged but never raised — a failed admin ping must not break a
webhook. PII (phones) is redacted by callers before being passed in.
"""

from __future__ import annotations

import config
from logger import get_logger

log = get_logger("notifier")

_API = "https://api.telegram.org/bot{token}/sendMessage"


def send_admin(text: str) -> bool:
    """Send a message to the configured admin chat. Returns True on success."""
    token = config.TELEGRAM_BOT_TOKEN
    chat_id = config.TELEGRAM_ADMIN_CHAT_ID
    if not (token and chat_id):
        log.warning("Telegram not configured — admin notification skipped.")
        return False
    try:
        import requests

        resp = requests.post(
            _API.format(token=token),
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            log.error("Telegram sendMessage failed: %s %s",
                      resp.status_code, resp.text[:200])
            return False
        log.info("Sent admin notification (%d chars).", len(text))
        return True
    except Exception as exc:  # noqa: BLE001 - never crash on notify
        log.error("Telegram notification error: %s", exc)
        return False


def notify_error(context: str, detail: str = "") -> None:
    """Convenience helper to alert the admin about an error."""
    msg = f"⚠️ <b>Ajo bot error</b>\n{context}"
    if detail:
        msg += f"\n<code>{detail[:500]}</code>"
    send_admin(msg)
