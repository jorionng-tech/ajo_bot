"""
reminders.py — Monthly contribution reminders (Section 7.4).

Run on a schedule (cron / Task Scheduler). By default it only acts when today is
one of `reminder_days_before` days before `monthly_due_day`, so it is safe to run
daily. Use --force to send regardless, and --dry-run to preview without sending.

  python reminders.py            # send if today is a reminder day
  python reminders.py --force    # send now regardless of the date
  python reminders.py --dry-run  # show who would be reminded, send nothing

Flow:
  1. Read all Active members.
  2. Determine the current cycle month (YYYY-MM, local time).
  3. Read which phones already paid this cycle.
  4. WhatsApp-remind each Active member who hasn't paid.
  5. Telegram a summary to the admin (paid/unpaid counts + redacted defaulters).
"""

from __future__ import annotations

import argparse
import sys

import config
import notifier
import sheets
import whatsapp
from logger import get_logger, redact_phone

log = get_logger("reminders")


def should_run_today() -> bool:
    """True if today is one of the configured reminder days before the due day."""
    today = config.now_local().day
    due = config.MONTHLY_DUE_DAY
    days_before = due - today
    return days_before in set(config.REMINDER_DAYS_BEFORE)


def _reminder_text(name: str) -> str:
    first = (name or "").strip().split()[0] if name else "there"
    return (
        f"Hi {first}, your {config.PLAN_NAME} contribution of "
        f"{config.format_money(config.PLAN_AMOUNT)} is due on the "
        f"{config.MONTHLY_DUE_DAY}th. Pay here: {config.PAYSTACK_PAYMENT_LINK}.\n"
        f"Reply {config.BALANCE_KEYWORD} to see your history."
    )


def run(force: bool = False, dry_run: bool = False) -> int:
    """Execute the reminder pass. Returns the number of reminders sent (or would send)."""
    if not (force or dry_run) and not should_run_today():
        log.info("Not a reminder day (due day %s, reminder days %s) — exiting.",
                 config.MONTHLY_DUE_DAY, config.REMINDER_DAYS_BEFORE)
        return 0

    cycle = config.current_cycle()
    log.info("Running reminders for cycle %s (dry_run=%s).", cycle, dry_run)

    active = sheets.get_active_members()
    paid_phones = sheets.phones_paid_in_cycle(cycle)

    unpaid = []
    for m in active:
        phone = str(m.get("Phone", "")).strip()
        if not phone or phone in paid_phones:
            continue
        unpaid.append(m)

    sent = 0
    for m in unpaid:
        phone = str(m.get("Phone", "")).strip()
        name = str(m.get("Name", "")).strip()
        if dry_run:
            log.info("[dry-run] would remind %s", redact_phone(phone))
            sent += 1
            continue
        if whatsapp.send(phone, _reminder_text(name)):
            sent += 1
        log.info("Reminder sent to %s", redact_phone(phone))

    # Admin summary with redacted defaulter phones.
    defaulters = "\n".join(
        f"- {redact_phone(str(m.get('Phone', '')).strip())}" for m in unpaid
    ) or "- none"
    summary = (
        f"📅 {config.GROUP_NAME} reminder run — cycle {cycle}\n"
        f"Active members: {len(active)}\n"
        f"Paid this cycle: {len(active) - len(unpaid)}\n"
        f"Unpaid: {len(unpaid)}\n"
        f"{'(dry-run, nothing sent)' if dry_run else f'Reminders sent: {sent}'}\n"
        f"Defaulters:\n{defaulters}"
    )
    if dry_run:
        log.info("[dry-run] admin summary:\n%s", summary)
    else:
        notifier.send_admin(summary)

    log.info("Reminder pass complete: %d reminder(s).", sent)
    return sent


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Ajo monthly reminders")
    parser.add_argument("--force", action="store_true",
                        help="Send regardless of today's date")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview only; send nothing")
    args = parser.parse_args(argv)

    if not args.dry_run:
        config.validate(strict=True)

    try:
        run(force=args.force, dry_run=args.dry_run)
        return 0
    except Exception as exc:  # noqa: BLE001
        log.error("Reminder run failed: %s", exc)
        notifier.notify_error("Reminder run failed", str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
