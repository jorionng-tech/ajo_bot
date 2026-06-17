"""
paystack.py — Paystack webhook signature validation + event parsing (Section 9.1).

The webhook signature is HMAC-SHA512 of the RAW request body, keyed by
PAYSTACK_WEBHOOK_SECRET, sent in the X-Paystack-Signature header. We compare with
hmac.compare_digest (constant time). The raw body MUST be read before parsing.

This module does no network I/O and imports cleanly without credentials.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Optional

import config
from logger import get_logger

log = get_logger("paystack")

CHARGE_SUCCESS = "charge.success"


def verify_signature(raw_body: bytes, signature: Optional[str]) -> bool:
    """Return True iff the X-Paystack-Signature matches HMAC-SHA512(raw_body, secret).

    raw_body must be the exact bytes received (not re-serialized JSON).
    """
    secret = config.PAYSTACK_WEBHOOK_SECRET
    if not secret:
        log.error("PAYSTACK_WEBHOOK_SECRET not configured — rejecting webhook.")
        return False
    if not signature:
        log.warning("Paystack webhook missing signature header.")
        return False
    if isinstance(raw_body, str):
        raw_body = raw_body.encode("utf-8")

    computed = hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha512
    ).hexdigest()
    ok = hmac.compare_digest(computed, signature.strip())
    if not ok:
        log.warning("Paystack signature mismatch — rejecting.")
    return ok


def parse_event(raw_body: bytes) -> Optional[dict]:
    """Parse the JSON webhook body. Returns the dict, or None if unparseable."""
    try:
        if isinstance(raw_body, bytes):
            raw_body = raw_body.decode("utf-8")
        return json.loads(raw_body)
    except (ValueError, UnicodeDecodeError) as exc:
        log.error("Failed to parse Paystack webhook body: %s", exc)
        return None


def is_charge_success(event: dict) -> bool:
    return bool(event) and event.get("event") == CHARGE_SUCCESS


def _to_naira(amount_kobo: Any) -> Optional[float]:
    """Paystack reports amounts in kobo (minor units). Convert to naira."""
    try:
        value = float(amount_kobo)
    except (TypeError, ValueError):
        return None
    naira = value / 100.0
    # Return an int when it's whole, to keep the sheet tidy.
    return int(naira) if naira.is_integer() else naira


def extract_charge(event: dict) -> dict:
    """Extract the fields we need from a charge.success event.

    Returns a dict with: reference, email, amount (naira), paid_at, phone, name.
    phone/name come from metadata when present (may be None).
    """
    data = (event or {}).get("data", {}) or {}
    customer = data.get("customer", {}) or {}
    metadata = data.get("metadata", {}) or {}

    # Metadata can be a JSON string in some Paystack configurations.
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            metadata = {}

    email = (customer.get("email") or metadata.get("email") or "").strip().lower()

    # Name: prefer metadata, fall back to customer first/last name.
    name = (metadata.get("name") or "").strip()
    if not name:
        first = (customer.get("first_name") or "").strip()
        last = (customer.get("last_name") or "").strip()
        name = (first + " " + last).strip()

    phone = (
        metadata.get("phone")
        or customer.get("phone")
        or ""
    )
    phone = str(phone).strip()

    return {
        "reference": str(data.get("reference") or "").strip(),
        "email": email,
        "amount": _to_naira(data.get("amount")),
        "paid_at": (data.get("paid_at") or data.get("paidAt") or "").strip(),
        "phone": phone or None,
        "name": name or None,
    }
