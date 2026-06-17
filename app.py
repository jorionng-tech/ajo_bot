"""
app.py — Flask application.

Two webhooks:
  POST /whatsapp/webhook  — all inbound WhatsApp (JOIN flow + BALANCE keyword).
  POST /paystack/webhook  — Paystack charge.success contribution confirmations.
  GET  /health            — liveness probe.

Security (Section 9):
  - Twilio signature validated against PUBLIC_WEBHOOK_URL (403 on mismatch).
  - Paystack HMAC-SHA512 validated on the RAW body (401 on mismatch).
  - Idempotency: MessageSid (in-memory bounded) + Paystack reference (sheet).
  - Per-number rate limiting on inbound WhatsApp.
  - debug=False in production; webhooks return 200 fast; no stack traces leak.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from datetime import datetime

from flask import Flask, request

import config
import conversation
import emailer
import notifier
import paystack
import sheets
import whatsapp
from logger import get_logger, redact_phone

log = get_logger("app")

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Phone normalization + validation (Section 9.4)
# ---------------------------------------------------------------------------
def normalize_phone(raw: str) -> str:
    """Strip a 'whatsapp:' prefix and surrounding space; keep E.164 '+' form."""
    if not raw:
        return ""
    s = str(raw).strip()
    if s.lower().startswith("whatsapp:"):
        s = s.split(":", 1)[1].strip()
    return s


def is_valid_phone(phone: str) -> bool:
    """E.164-ish: optional '+', digits only, at least 7 digits."""
    if not phone:
        return False
    digits = re.sub(r"\D", "", phone)
    return len(digits) >= 7


# ---------------------------------------------------------------------------
# Idempotency store for Twilio MessageSid (bounded, thread-safe)
# ---------------------------------------------------------------------------
class _SeenStore:
    def __init__(self, maxlen: int = 2000):
        self._set: set[str] = set()
        self._order: deque[str] = deque()
        self._max = maxlen
        self._lock = threading.Lock()

    def seen(self, key: str) -> bool:
        """Return True if key was already seen; otherwise record it and return False."""
        if not key:
            return False
        with self._lock:
            if key in self._set:
                return True
            self._set.add(key)
            self._order.append(key)
            if len(self._order) > self._max:
                old = self._order.popleft()
                self._set.discard(old)
            return False


_seen_messages = _SeenStore()


# ---------------------------------------------------------------------------
# Per-number rate limiter (Section 9.5): ~10 inbound / 60s
# ---------------------------------------------------------------------------
class _RateLimiter:
    def __init__(self, limit: int = 10, window: int = 60):
        self._limit = limit
        self._window = window
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            dq = self._hits.setdefault(key, deque())
            while dq and now - dq[0] > self._window:
                dq.popleft()
            if len(dq) >= self._limit:
                return False
            dq.append(now)
            return True


_rate = _RateLimiter()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok", "group": config.GROUP_NAME}, 200


# ---------------------------------------------------------------------------
# WhatsApp webhook (Section 8 routing)
# ---------------------------------------------------------------------------
@app.post("/whatsapp/webhook")
def whatsapp_webhook():
    # 1. Signature validation
    signature = request.headers.get("X-Twilio-Signature")
    params = request.form.to_dict()
    if not whatsapp.validate_signature(signature, params):
        return ("Forbidden", 403)

    message_sid = params.get("MessageSid", "")
    raw_from = params.get("From", "")
    body = params.get("Body", "") or ""
    phone = normalize_phone(raw_from)

    # 1b. Idempotency on MessageSid
    if _seen_messages.seen(message_sid):
        log.info("Duplicate MessageSid ignored.")
        return ("", 200)

    if not is_valid_phone(phone):
        log.warning("Inbound WhatsApp with invalid phone — ignoring.")
        return ("", 200)

    # Rate limit per number across all inbound.
    if not _rate.allow(phone):
        log.warning("Rate limit hit for %s — ignoring.", redact_phone(phone))
        return ("", 200)

    try:
        reply = _route_whatsapp(phone, body)
    except Exception as exc:  # noqa: BLE001 - never 500 to Twilio
        log.error("Error handling WhatsApp from %s: %s", redact_phone(phone), exc)
        notifier.notify_error("WhatsApp handler error", str(exc))
        return ("", 200)

    if reply:
        whatsapp.send(phone, reply)
    return ("", 200)


def _route_whatsapp(phone: str, body: str):
    """Apply Section 8 routing. Returns reply text or None."""
    text = (body or "").strip()

    # 2. Active JOIN conversation takes priority.
    if conversation.is_in_flow(phone):
        return conversation.handle(phone, text)

    upper = text.upper()
    # 3. JOIN keyword
    if upper == config.JOIN_KEYWORD.strip().upper():
        return conversation.start_join(phone)

    # 4. BALANCE keyword
    if upper == config.BALANCE_KEYWORD.strip().upper():
        return _balance_reply(phone)

    # 5. Otherwise ignore silently.
    return None


# ---------------------------------------------------------------------------
# BALANCE flow (Section 7.3)
# ---------------------------------------------------------------------------
def _to_float(value) -> float:
    try:
        return float(str(value).replace(",", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _balance_reply(phone: str) -> str:
    member = sheets.find_member_by_phone(phone)
    if not member:
        return ("Your number isn't registered yet. "
                f"Text {config.JOIN_KEYWORD} to become a member.")

    name = str(member.get("Name", "")).strip() or "there"
    contribs = [
        c for c in sheets.get_contributions_by_phone(phone)
        if str(c.get("Status", "")).strip().lower() == sheets.CONTRIB_CONFIRMED.lower()
    ]

    total = sum(_to_float(c.get("Amount")) for c in contribs)
    count = len(contribs)

    # Last payment by Payment Date (string YYYY-MM-DD sorts correctly).
    last_line = "—"
    if contribs:
        last = max(contribs, key=lambda c: str(c.get("Payment Date", "")))
        last_line = (f"{last.get('Payment Date', '?')} "
                     f"({config.format_money(_to_float(last.get('Amount')))})")

    cycle = config.current_cycle()
    paid_this_cycle = any(
        str(c.get("Cycle Month", "")).strip() == cycle for c in contribs
    )

    return (
        f"Hi {name}, your Ajo summary:\n"
        f"- Total contributed: {config.format_money(total)}\n"
        f"- Cycles paid: {count}\n"
        f"- Last payment: {last_line}\n"
        f"- This month ({cycle}): {'Paid' if paid_this_cycle else 'Not yet'}"
    )


# ---------------------------------------------------------------------------
# Paystack webhook (Section 7.2)
# ---------------------------------------------------------------------------
@app.post("/paystack/webhook")
def paystack_webhook():
    # 1. RAW body BEFORE parsing, validate HMAC-SHA512 signature.
    raw_body = request.get_data()
    signature = request.headers.get("X-Paystack-Signature")
    if not paystack.verify_signature(raw_body, signature):
        return ("Unauthorized", 401)

    event = paystack.parse_event(raw_body)
    if not event:
        return ("", 200)  # unparseable but signed; ack to stop retries

    # 2. Only handle charge.success.
    if not paystack.is_charge_success(event):
        return ("", 200)

    charge = paystack.extract_charge(event)
    reference = charge["reference"]
    if not reference:
        log.warning("charge.success with no reference — ignoring.")
        return ("", 200)

    try:
        _process_charge(charge)
    except Exception as exc:  # noqa: BLE001 - never 500 to Paystack
        log.error("Error processing charge ref=%s: %s", reference, exc)
        notifier.notify_error(f"Paystack processing error (ref {reference})", str(exc))

    # 10. Always 200 once signature is valid (providers retry on non-200).
    return ("", 200)


def _cycle_and_dates(paid_at: str):
    """Return (payment_date 'YYYY-MM-DD', cycle 'YYYY-MM') from a Paystack timestamp."""
    if paid_at:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S.%f%z",
                    "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z",
                    "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(paid_at, fmt)
                return dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m")
            except ValueError:
                continue
    now = config.now_local()
    return now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")


def _process_charge(charge: dict) -> None:
    reference = charge["reference"]
    email = charge["email"]
    amount = charge["amount"]
    meta_phone = normalize_phone(charge.get("phone") or "")
    meta_name = charge.get("name") or ""

    # 3. Idempotency — already recorded?
    if sheets.reference_exists(reference):
        log.info("Duplicate Paystack reference ref=%s — skipping.", reference)
        return

    # 5 (validation). Reject non-positive amounts.
    if not amount or amount <= 0:
        log.warning("Non-positive amount on ref=%s — flagging admin.", reference)
        notifier.notify_error(
            f"Payment with invalid amount (ref {reference})",
            f"amount={amount}, email={email}",
        )
        return

    payment_date, cycle = _cycle_and_dates(charge.get("paid_at", ""))

    # 5. Match member by EMAIL.
    member = sheets.find_member_by_email(email) if email else None

    member_phone = ""
    member_name = ""

    if member:
        member_phone = normalize_phone(str(member.get("Phone", "")).strip())
        member_name = str(member.get("Name", "")).strip()
        status = str(member.get("Status", "")).strip().lower()

        if status == sheets.STATUS_PENDING.lower():
            sheets.update_member_status(
                email=email, status=sheets.STATUS_ACTIVE,
                join_date=payment_date,
                ensure_phone=member_phone or meta_phone,
            )
            if not member_phone:
                member_phone = meta_phone
        elif status == sheets.STATUS_ACTIVE.lower():
            pass  # renewal — nothing to change on the member row
        elif status == sheets.STATUS_INACTIVE.lower():
            sheets.update_member_status(email=email, status=sheets.STATUS_ACTIVE)
            notifier.send_admin(
                f"♻️ Reactivated member after payment (ref {reference}).\n"
                f"Phone: {redact_phone(member_phone)}"
            )
        else:
            # Unknown/blank status — treat as activation.
            sheets.update_member_status(
                email=email, status=sheets.STATUS_ACTIVE, join_date=payment_date,
                ensure_phone=member_phone or meta_phone,
            )
    else:
        # 5d. Paid without prior JOIN — create an Active member from payment data.
        member_name = meta_name or (email.split("@")[0] if email else "Unknown")
        member_phone = meta_phone
        sheets.add_member(
            name=member_name,
            phone=member_phone,
            email=email,
            status=sheets.STATUS_ACTIVE,
            plan=config.PLAN_NAME,
            monthly_amount=config.PLAN_AMOUNT,
            join_date=payment_date,
            notes="Auto-created from payment (no prior JOIN)",
        )
        notifier.send_admin(
            f"⚠️ Payment received with no prior JOIN — please review.\n"
            f"Name: {member_name}\nEmail: {email}\n"
            f"Phone: {redact_phone(member_phone) if member_phone else 'unknown'}\n"
            f"Ref: {reference}"
        )

    # 6. Append the contribution (idempotency already checked).
    sheets.add_contribution(
        reference=reference,
        member_phone=member_phone,
        member_name=member_name,
        amount=amount,
        payment_date=payment_date,
        cycle_month=cycle,
        status=sheets.CONTRIB_CONFIRMED,
    )

    # 7. WhatsApp receipt (only if we know the phone).
    if member_phone and is_valid_phone(member_phone):
        whatsapp.send(
            member_phone,
            (f"✅ Payment received — thank you{(' ' + member_name) if member_name else ''}!\n"
             f"{config.format_money(amount)} for {config.PLAN_NAME} "
             f"({config.GROUP_NAME}).\n"
             f"Cycle: {cycle}. Ref: {reference}.\n"
             f"Reply {config.BALANCE_KEYWORD} to see your history."),
        )
    else:
        log.info("No phone for ref=%s — WhatsApp receipt skipped.", reference)
        notifier.send_admin(
            f"ℹ️ Payment confirmed but no phone on file — WhatsApp receipt skipped.\n"
            f"Ref: {reference}, Email: {email}"
        )

    # 8. Email receipt (optional).
    if emailer.is_enabled() and email:
        emailer.send_receipt(
            to_email=email,
            to_name=member_name,
            subject=f"Your {config.GROUP_NAME} contribution receipt",
            text_body=(
                f"Hi {member_name or 'there'},\n\n"
                f"We received your contribution of {config.format_money(amount)} "
                f"for {config.PLAN_NAME} ({config.GROUP_NAME}).\n"
                f"Cycle: {cycle}\nReference: {reference}\nDate: {payment_date}\n\n"
                f"Thank you!"
            ),
        )

    # 9. Admin summary.
    notifier.send_admin(
        f"💰 Contribution confirmed for {config.GROUP_NAME}\n"
        f"Name: {member_name or 'Unknown'}\n"
        f"Phone: {redact_phone(member_phone) if member_phone else 'unknown'}\n"
        f"Amount: {config.format_money(amount)}\n"
        f"Cycle: {cycle}\nRef: {reference}"
    )


if __name__ == "__main__":
    # Fail loud if misconfigured (Section 0.6).
    config.validate(strict=True)
    app.run(host="0.0.0.0", port=config.PORT, debug=not config.IS_PRODUCTION)
