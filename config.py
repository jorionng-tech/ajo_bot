"""
config.py — Loads and validates configuration.

Two sources of configuration:
  1. Secrets / environment-specific values  -> environment variables (.env)
  2. Group-configurable business values     -> ajo_config.json

Design notes:
- Importing this module NEVER fails, even with no .env present. Values simply
  load as None / defaults. This lets every other module import cleanly during
  development before credentials are wired up (per build instructions).
- Call validate() explicitly at app startup (app.py / reminders.py) to fail
  loudly when required credentials are missing.
"""

from __future__ import annotations

import json
import os

from logger import get_logger

try:
    from dotenv import load_dotenv

    # Loads .env if present; silently does nothing otherwise.
    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional at runtime
    pass

log = get_logger("config")

# ---------------------------------------------------------------------------
# Environment variables (.env) — Section 4
# ---------------------------------------------------------------------------

# -- Paystack --
PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY")
PAYSTACK_WEBHOOK_SECRET = os.environ.get("PAYSTACK_WEBHOOK_SECRET")

# -- Google Sheets --
GOOGLE_SPREADSHEET_ID = os.environ.get("GOOGLE_SPREADSHEET_ID")
GOOGLE_CREDENTIALS_FILE = os.environ.get("GOOGLE_CREDENTIALS_FILE", "credentials.json")

# -- Twilio WhatsApp --
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM")
PUBLIC_WEBHOOK_URL = os.environ.get("PUBLIC_WEBHOOK_URL")

# -- Telegram --
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID")

# -- Brevo (optional) --
BREVO_API_KEY = os.environ.get("BREVO_API_KEY")
BREVO_SENDER_EMAIL = os.environ.get("BREVO_SENDER_EMAIL")
BREVO_SENDER_NAME = os.environ.get("BREVO_SENDER_NAME")

# -- App --
FLASK_ENV = os.environ.get("FLASK_ENV", "production")
PORT = int(os.environ.get("PORT", "5002"))
IS_PRODUCTION = FLASK_ENV.lower() == "production"

# ---------------------------------------------------------------------------
# ajo_config.json — Section 6
# ---------------------------------------------------------------------------

AJO_CONFIG_FILE = os.environ.get("AJO_CONFIG_FILE", "ajo_config.json")

_DEFAULT_AJO_CONFIG = {
    "group_name": "Ajo Group",
    "currency": "NGN",
    "plan": {
        "name": "Monthly Ajo",
        "monthly_amount": 0,
        "paystack_payment_link": "",
    },
    "monthly_due_day": 25,
    "reminder_days_before": [5, 2],
    "timezone": "Africa/Lagos",
    "join_keyword": "JOIN",
    "balance_keyword": "BALANCE",
    "send_email_receipt": False,
    "admin_name": "Secretary",
}


def _load_ajo_config() -> dict:
    """Load ajo_config.json, falling back to defaults if absent/unreadable."""
    try:
        with open(AJO_CONFIG_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        # Shallow-merge over defaults so missing keys don't crash callers.
        merged = dict(_DEFAULT_AJO_CONFIG)
        merged.update(data)
        # Ensure nested plan keys exist.
        plan = dict(_DEFAULT_AJO_CONFIG["plan"])
        plan.update(data.get("plan", {}))
        merged["plan"] = plan
        return merged
    except FileNotFoundError:
        log.warning("%s not found — using defaults. Create it before going live.",
                    AJO_CONFIG_FILE)
        return dict(_DEFAULT_AJO_CONFIG)
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Failed to read %s (%s) — using defaults.", AJO_CONFIG_FILE, exc)
        return dict(_DEFAULT_AJO_CONFIG)


AJO = _load_ajo_config()

# Convenience accessors (read-only views over AJO).
GROUP_NAME = AJO["group_name"]
CURRENCY = AJO["currency"]
PLAN_NAME = AJO["plan"]["name"]
PLAN_AMOUNT = AJO["plan"]["monthly_amount"]
PAYSTACK_PAYMENT_LINK = AJO["plan"]["paystack_payment_link"]
MONTHLY_DUE_DAY = AJO["monthly_due_day"]
REMINDER_DAYS_BEFORE = AJO["reminder_days_before"]
TIMEZONE = AJO["timezone"]
JOIN_KEYWORD = AJO["join_keyword"]
BALANCE_KEYWORD = AJO["balance_keyword"]
SEND_EMAIL_RECEIPT = bool(AJO["send_email_receipt"])
ADMIN_NAME = AJO["admin_name"]

# Sheet/tab names — fixed per Section 5.
MEMBERS_SHEET = "Members"
CONTRIBUTIONS_SHEET = "Contributions"


# ---------------------------------------------------------------------------
# Validation — call explicitly at startup, not at import.
# ---------------------------------------------------------------------------

# (env var name, human description) for each hard requirement.
_REQUIRED_ENV = [
    ("PAYSTACK_SECRET_KEY", "Paystack secret key"),
    ("PAYSTACK_WEBHOOK_SECRET", "Paystack webhook secret"),
    ("GOOGLE_SPREADSHEET_ID", "Google spreadsheet ID"),
    ("GOOGLE_CREDENTIALS_FILE", "Google credentials file path"),
    ("TWILIO_ACCOUNT_SID", "Twilio account SID"),
    ("TWILIO_AUTH_TOKEN", "Twilio auth token"),
    ("TWILIO_WHATSAPP_FROM", "Twilio WhatsApp 'from' number"),
    ("PUBLIC_WEBHOOK_URL", "Public webhook URL (for Twilio signature check)"),
]

# Telegram is required for admin notifications (fail-loud principle, Section 0.6).
_REQUIRED_ENV += [
    ("TELEGRAM_BOT_TOKEN", "Telegram bot token"),
    ("TELEGRAM_ADMIN_CHAT_ID", "Telegram admin chat ID"),
]


def missing_required() -> list[str]:
    """Return a list of human-readable descriptions of missing required config."""
    missing = []
    for name, desc in _REQUIRED_ENV:
        if not os.environ.get(name):
            missing.append(f"{name} ({desc})")

    # credentials.json must exist on disk for Sheets to work.
    if GOOGLE_CREDENTIALS_FILE and not os.path.exists(GOOGLE_CREDENTIALS_FILE):
        missing.append(
            f"{GOOGLE_CREDENTIALS_FILE} (Google service account key file not found)"
        )

    # Brevo only required if email receipts are enabled.
    if SEND_EMAIL_RECEIPT:
        for name, desc in [
            ("BREVO_API_KEY", "Brevo API key"),
            ("BREVO_SENDER_EMAIL", "Brevo sender email"),
        ]:
            if not os.environ.get(name):
                missing.append(f"{name} ({desc}) — required when send_email_receipt=true")

    # Plan must be configured.
    if not PLAN_AMOUNT or PLAN_AMOUNT <= 0:
        missing.append("ajo_config.json plan.monthly_amount (must be > 0)")
    if not PAYSTACK_PAYMENT_LINK:
        missing.append("ajo_config.json plan.paystack_payment_link (must be set)")

    return missing


def now_local():
    """Return the current datetime in the configured timezone."""
    from datetime import datetime

    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(TIMEZONE))
    except Exception:  # pragma: no cover - bad tz name / missing tzdata
        return datetime.now()


def current_cycle() -> str:
    """Return the current contribution cycle month as 'YYYY-MM' in local time."""
    return now_local().strftime("%Y-%m")


_CURRENCY_SYMBOL = {"NGN": "₦", "GHS": "₵", "KES": "KSh", "USD": "$"}


def format_money(amount) -> str:
    """Format an amount with the configured currency, e.g. '₦50,000' for NGN."""
    try:
        n = float(amount)
        num = f"{int(n):,}" if n.is_integer() else f"{n:,.2f}"
    except (TypeError, ValueError):
        num = str(amount)
    symbol = _CURRENCY_SYMBOL.get(CURRENCY.upper())
    if symbol:
        return f"{symbol}{num}"
    return f"{CURRENCY} {num}"


def validate(strict: bool = True) -> list[str]:
    """Validate configuration at startup.

    Logs each missing item. If strict, raises RuntimeError so the process won't
    start half-configured (fail loud, Section 0.6). Returns the list of missing
    items either way.
    """
    # C-3: A Paystack TEST key (sk_test_) accepts test cards, so running one in
    # production would let fake payments activate memberships. Fail hard.
    if IS_PRODUCTION and (PAYSTACK_SECRET_KEY or "").startswith("sk_test_"):
        log.critical(
            "FATAL: PAYSTACK_SECRET_KEY is a TEST key (sk_test_) while "
            "FLASK_ENV=production. Refusing to start — use a live key."
        )
        raise SystemExit(1)

    missing = missing_required()
    if missing:
        for item in missing:
            log.error("Missing required configuration: %s", item)
        if strict:
            raise RuntimeError(
                "Configuration incomplete — missing: " + "; ".join(missing)
            )
    else:
        log.info("Configuration validated: all required values present.")
    return missing


if __name__ == "__main__":
    # Lets you run `python config.py` to see what's missing.
    problems = validate(strict=False)
    if problems:
        print("Missing configuration:")
        for p in problems:
            print(f"  - {p}")
    else:
        print("All required configuration present.")
