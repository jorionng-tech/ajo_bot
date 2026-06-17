"""
conversation.py — JOIN flow state machine (Section 7.1).

State is an in-memory dict keyed by WhatsApp phone (Tier 1; resets on restart,
which is acceptable — Redis is the documented Tier 2 upgrade). Access is guarded
by a lock for thread safety.

handle() / start_join() return the member-facing reply text (a string), or None
to send nothing. Side effects (Sheets writes, Telegram admin notifications) happen
inside this module; the caller is responsible only for delivering the returned
reply over WhatsApp.
"""

from __future__ import annotations

import re
import threading
from datetime import date

import config
import notifier
import sheets
from logger import get_logger, redact_phone

log = get_logger("conversation")

# Conversation states
AWAITING_CONFIRM = "AWAITING_CONFIRM"
AWAITING_NAME = "AWAITING_NAME"
AWAITING_EMAIL = "AWAITING_EMAIL"

# Validation limits
MAX_NAME_LEN = 80
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

# phone -> {"state": str, "name": str, "email_retry": bool}
_states: dict[str, dict] = {}
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Validation helpers (Section 9.4)
# ---------------------------------------------------------------------------
def validate_name(raw: str) -> tuple[bool, str]:
    """Return (ok, cleaned_name). Rejects empties, control chars, over-length."""
    if raw is None:
        return False, ""
    cleaned = str(raw).strip()
    if not cleaned:
        return False, ""
    if _CONTROL_RE.search(cleaned):
        return False, ""
    if len(cleaned) > MAX_NAME_LEN:
        cleaned = cleaned[:MAX_NAME_LEN].strip()
    # Require at least one letter so "12345" isn't accepted as a name.
    if not any(ch.isalpha() for ch in cleaned):
        return False, ""
    return True, cleaned


def validate_email(raw: str) -> tuple[bool, str]:
    """Return (ok, normalized_email)."""
    if not raw:
        return False, ""
    cleaned = str(raw).strip().lower()
    if len(cleaned) > 254 or not _EMAIL_RE.match(cleaned):
        return False, ""
    return True, cleaned


def _first_name(full_name: str) -> str:
    return full_name.strip().split()[0] if full_name.strip() else full_name


# ---------------------------------------------------------------------------
# State access
# ---------------------------------------------------------------------------
def is_in_flow(phone: str) -> bool:
    with _lock:
        return phone in _states


def _set(phone: str, **kwargs) -> None:
    with _lock:
        cur = _states.get(phone, {})
        cur.update(kwargs)
        _states[phone] = cur


def _get(phone: str) -> dict:
    with _lock:
        return dict(_states.get(phone, {}))


def reset(phone: str) -> None:
    with _lock:
        _states.pop(phone, None)


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------
def _welcome_msg() -> str:
    return (
        f"Welcome to {config.GROUP_NAME}!\n"
        f"Our Ajo plan:\n"
        f"- {config.PLAN_NAME} — {config.format_money(config.PLAN_AMOUNT)}/month\n"
        f"Reply YES to join, or STOP to cancel."
    )


def _payment_msg(first_name: str) -> str:
    return (
        f"Almost done, {first_name}!\n"
        f"Pay {config.format_money(config.PLAN_AMOUNT)} here to activate your membership:\n"
        f"{config.PAYSTACK_PAYMENT_LINK}\n"
        f"You'll get a confirmation the moment your payment lands.\n"
        f"Reply {config.BALANCE_KEYWORD} anytime to check your contributions."
    )


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def start_join(phone: str) -> str:
    """Handle a JOIN keyword from a phone not currently mid-flow (Section 7.1 edges)."""
    try:
        member = sheets.find_member_by_phone(phone)
    except Exception as exc:  # noqa: BLE001 - surface but stay up
        log.error("Sheets lookup failed during JOIN for %s: %s",
                  redact_phone(phone), exc)
        notifier.notify_error("JOIN lookup failed", str(exc))
        return "Sorry, we couldn't process that right now. Please try again shortly."

    status = str(member.get("Status", "")).strip().lower() if member else ""

    # Already an Active member -> recognize, don't restart the flow.
    if member and status == sheets.STATUS_ACTIVE.lower():
        return (
            f"You're already a member of {config.GROUP_NAME}. "
            f"Reply {config.BALANCE_KEYWORD} to see your contributions."
        )

    # Pending record exists -> resend the link, no duplicate row.
    if member and status == sheets.STATUS_PENDING.lower():
        log.info("JOIN from existing Pending member %s — resending link.",
                 redact_phone(phone))
        name = str(member.get("Name", "")).strip()
        return _payment_msg(_first_name(name) if name else "there")

    # Fresh start.
    _set(phone, state=AWAITING_CONFIRM, name="", email_retry=False)
    return _welcome_msg()


def handle(phone: str, body: str) -> str | None:
    """Advance the conversation for a phone already in a flow. Returns reply text."""
    body = (body or "").strip()
    st = _get(phone)
    state = st.get("state")

    if state == AWAITING_CONFIRM:
        if body.upper() == "YES":
            _set(phone, state=AWAITING_NAME)
            return "Great! What's your full name?"
        # STOP or anything else cancels.
        reset(phone)
        return "No problem. Text JOIN anytime to start."

    if state == AWAITING_NAME:
        ok, name = validate_name(body)
        if not ok:
            return "Please send your full name (letters only, no symbols)."
        _set(phone, state=AWAITING_EMAIL, name=name)
        return f"Thanks {_first_name(name)}! What email should we use for your receipts?"

    if state == AWAITING_EMAIL:
        ok, email = validate_email(body)
        if not ok:
            if not st.get("email_retry"):
                _set(phone, email_retry=True)
                return "That doesn't look like a valid email. Please try again."
            # Second failure — keep them in state but ask once more gently.
            return "Still not a valid email. Please send it like name@example.com."

        # Block duplicate registrations: if this email already exists, don't
        # write another row — respond based on its current status instead.
        try:
            existing = sheets.find_member_by_email(email)
        except Exception as exc:  # noqa: BLE001 - surface but stay up
            log.error("Email lookup failed during JOIN for %s: %s",
                      redact_phone(phone), exc)
            notifier.notify_error("JOIN email lookup failed", str(exc))
            return "Sorry, we couldn't process that right now. Please try again shortly."

        if existing:
            status = str(existing.get("Status", "")).strip().lower()
            reset(phone)
            if status == sheets.STATUS_PENDING.lower():
                return (
                    "You've already started registration with this email.\n"
                    "Complete your payment to activate your membership:\n"
                    f"{config.PAYSTACK_PAYMENT_LINK}"
                )
            if status == sheets.STATUS_ACTIVE.lower():
                return (
                    "You're already an active member!\n"
                    f"Text {config.BALANCE_KEYWORD} to check your contributions."
                )
            if status == sheets.STATUS_INACTIVE.lower():
                return (
                    "This email is linked to an inactive membership.\n"
                    "Please contact your secretary to reactivate."
                )

        return _finish(phone, st.get("name", ""), email)

    # Unknown state — clear it.
    log.warning("handle() with unknown state for %s; resetting.", redact_phone(phone))
    reset(phone)
    return None


def _finish(phone: str, name: str, email: str) -> str:
    """Persist the pending member, notify admin, and return the payment message."""
    first = _first_name(name) if name else "there"
    try:
        # Guard against a race/duplicate: if a row already exists, don't add another.
        existing = sheets.find_member_by_phone(phone)
        if existing:
            log.info("Member row already exists for %s — not duplicating.",
                     redact_phone(phone))
        else:
            sheets.add_member(
                name=name,
                phone=phone,
                email=email,
                status=sheets.STATUS_PENDING,
                plan=config.PLAN_NAME,
                monthly_amount=config.PLAN_AMOUNT,
                join_date="",
                notes="",
            )
        notifier.send_admin(
            f"🆕 New pending member for {config.GROUP_NAME}\n"
            f"Name: {name}\n"
            f"Phone: {redact_phone(phone)}\n"
            f"Plan: {config.PLAN_NAME} ({config.format_money(config.PLAN_AMOUNT)})\n"
            f"Awaiting payment."
        )
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to persist pending member %s: %s", redact_phone(phone), exc)
        notifier.notify_error("Failed to save pending member", str(exc))
        reset(phone)
        return ("Sorry, something went wrong saving your details. "
                "Please text JOIN to try again.")

    reset(phone)
    return _payment_msg(first)
