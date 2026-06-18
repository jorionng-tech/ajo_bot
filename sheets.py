"""
sheets.py — Google Sheets read/write wrapper (the database, Section 5).

Two tabs in one spreadsheet:
  - "Members"        : A Name, B Phone, C Email, D Status, E Plan,
                       F Monthly Amount, G Join Date, H Notes
  - "Contributions"  : A Reference, B Member Phone, C Member Name, D Amount,
                       E Payment Date, F Cycle Month, G Status, H Logged At

Security / safety:
- Append-only for contributions; member updates target a single matched row
  (Section 9.9).
- reference_exists() guards every contribution write (idempotency, Section 9.3).
- Cell values are sanitized to neutralize spreadsheet formula injection before
  writing (Section 9.4).

Connection is lazy: importing this module never touches the network or the
credentials file. The first real call builds the client and will raise loudly
if credentials are missing/invalid (fail loud, Section 0.6).
"""

from __future__ import annotations

import threading
from datetime import date, datetime
from typing import Any, Optional

import config
from logger import get_logger, redact_phone

log = get_logger("sheets")

# Scopes required for gspread to read/write the sheet. Sheets-only by design
# (H-3): the bot only touches its own spreadsheet (opened by key, shared with
# the service account), so the broad Drive scope is not needed.
_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]

# ---------------------------------------------------------------------------
# Column layout (1-based indexes for gspread cell updates)
# ---------------------------------------------------------------------------
MEMBER_HEADERS = [
    "Name", "Phone", "Email", "Status", "Plan", "Monthly Amount",
    "Join Date", "Notes",
]
MEMBER_COL = {name: i + 1 for i, name in enumerate(MEMBER_HEADERS)}

CONTRIB_HEADERS = [
    "Reference", "Member Phone", "Member Name", "Amount", "Payment Date",
    "Cycle Month", "Status", "Logged At",
]

# Status values
STATUS_PENDING = "Pending"
STATUS_ACTIVE = "Active"
STATUS_INACTIVE = "Inactive"
CONTRIB_CONFIRMED = "Confirmed"
CONTRIB_REFUNDED = "Refunded"

# ---------------------------------------------------------------------------
# Lazy connection
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_client = None
_spreadsheet = None


def _get_spreadsheet():
    """Return the cached gspread Spreadsheet, building the client on first use."""
    global _client, _spreadsheet
    if _spreadsheet is not None:
        return _spreadsheet
    with _lock:
        if _spreadsheet is not None:
            return _spreadsheet

        # Imported here so the module imports cleanly even if libs are absent.
        import gspread
        from google.oauth2.service_account import Credentials

        if not config.GOOGLE_SPREADSHEET_ID:
            raise RuntimeError("GOOGLE_SPREADSHEET_ID is not set.")
        creds = Credentials.from_service_account_file(
            config.GOOGLE_CREDENTIALS_FILE, scopes=_SCOPES
        )
        _client = gspread.authorize(creds)
        _spreadsheet = _client.open_by_key(config.GOOGLE_SPREADSHEET_ID)
        log.info("Connected to Google Spreadsheet.")
        return _spreadsheet


def _ws(title: str):
    """Return a worksheet (tab) by exact title."""
    return _get_spreadsheet().worksheet(title)


# ---------------------------------------------------------------------------
# Value sanitization (formula-injection defense, Section 9.4)
# ---------------------------------------------------------------------------
def _sanitize(value: Any) -> Any:
    """Neutralize spreadsheet formula injection for text cells.

    A leading =, +, -, or @ can make Sheets evaluate a cell as a formula. Prefix
    such strings with a single quote. Numbers are returned unchanged.
    """
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return value
    s = str(value)
    if s and s[0] in ("=", "+", "-", "@"):
        return "'" + s
    return s


# ---------------------------------------------------------------------------
# Value cleaning on read (apostrophe-prefix defense)
# ---------------------------------------------------------------------------
# Google Sheets prepends a leading apostrophe to cell values that start with
# + or = (to stop formula evaluation — see _sanitize above). gspread returns
# that apostrophe as part of the string, so a phone stored as +234... reads back
# as '+234..., breaking exact-match lookups. Strip it (and surrounding
# whitespace) from every string value we read before comparing or returning it.
_NUMERIC_COLS = {"Amount", "Monthly Amount"}


def _clean(value: Any) -> str:
    """Normalize a string cell read from Sheets: strip whitespace and a leading
    apostrophe that Sheets prepends to + / = values."""
    if value is None:
        return ""
    return str(value).strip().lstrip("'")


def _clean_record(rec: dict) -> dict:
    """Return a copy of a row dict with all string columns cleaned. Numeric
    columns (Amount, Monthly Amount) are left untouched."""
    return {
        key: (val if key in _NUMERIC_COLS else _clean(val))
        for key, val in rec.items()
    }


def _today_str() -> str:
    return date.today().strftime("%Y-%m-%d")


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------
def _records(title: str) -> list[dict]:
    """Return all data rows of a tab as dicts keyed by header."""
    return _ws(title).get_all_records()


def find_member_by_phone(phone: str) -> Optional[dict]:
    """Return the member record matching this phone exactly, or None."""
    if not phone:
        return None
    target = _clean(phone).lstrip('+')
    for rec in _records(config.MEMBERS_SHEET):
        cleaned_sheet = _clean(rec.get("Phone", "")).lstrip('+')
        
        if cleaned_sheet == target:
            return _clean_record(rec)
    return None


def find_member_by_email(email: str) -> Optional[dict]:
    """Return the member record matching this email (case-insensitive), or None."""
    if not email:
        return None
    target = _clean(email).lower()
    for rec in _records(config.MEMBERS_SHEET):
        if _clean(rec.get("Email", "")).lower() == target:
            return _clean_record(rec)
    return None


def _find_member_row_index(match_col: str, value: str) -> Optional[int]:
    """Return the 1-based sheet row number of the member matching value, else None.

    Matching is exact (case-insensitive for Email). Row 1 is the header.
    """
    ws = _ws(config.MEMBERS_SHEET)
    col_idx = MEMBER_COL[match_col]
    values = ws.col_values(col_idx)  # includes header at index 0
    norm = (lambda s: _clean(s).lower()) if match_col == "Email" else \
           (lambda s: _clean(s))
    target = norm(value)
    for i, cell in enumerate(values[1:], start=2):  # data starts at row 2
        if norm(cell) == target:
            return i
    return None


def add_member(
    name: str,
    phone: str,
    email: str,
    status: str = STATUS_PENDING,
    plan: Optional[str] = None,
    monthly_amount: Optional[float] = None,
    join_date: str = "",
    notes: str = "",
) -> None:
    """Append a new member row."""
    plan = plan if plan is not None else config.PLAN_NAME
    monthly_amount = monthly_amount if monthly_amount is not None else config.PLAN_AMOUNT
    row = [
        _sanitize(name),
        _sanitize(phone),
        _sanitize(email),
        _sanitize(status),
        _sanitize(plan),
        _sanitize(monthly_amount),
        _sanitize(join_date),
        _sanitize(notes),
    ]
    _ws(config.MEMBERS_SHEET).append_row(row, value_input_option="USER_ENTERED")
    log.info("Added member phone=%s status=%s", redact_phone(phone), status)


def update_member_status(
    *,
    phone: Optional[str] = None,
    email: Optional[str] = None,
    status: str,
    join_date: Optional[str] = None,
    ensure_phone: Optional[str] = None,
) -> bool:
    """Update a single matched member row's Status (and optionally Join Date / Phone).

    Match by phone if given, else by email. Returns True if a row was updated.
    Only the matched row is touched (Section 9.9).
    """
    if phone:
        row_idx = _find_member_row_index("Phone", phone)
    elif email:
        row_idx = _find_member_row_index("Email", email)
    else:
        raise ValueError("update_member_status requires phone or email")

    if not row_idx:
        return False

    ws = _ws(config.MEMBERS_SHEET)
    ws.update_cell(row_idx, MEMBER_COL["Status"], _sanitize(status))
    if join_date is not None:
        ws.update_cell(row_idx, MEMBER_COL["Join Date"], _sanitize(join_date))
    if ensure_phone:
        current = ws.cell(row_idx, MEMBER_COL["Phone"]).value
        if not (current and str(current).strip()):
            ws.update_cell(row_idx, MEMBER_COL["Phone"], _sanitize(ensure_phone))
    log.info("Updated member row=%s -> status=%s", row_idx, status)
    return True


def get_active_members() -> list[dict]:
    """Return all member records whose Status is Active."""
    return [
        _clean_record(rec) for rec in _records(config.MEMBERS_SHEET)
        if _clean(rec.get("Status", "")).lower() == STATUS_ACTIVE.lower()
    ]


# ---------------------------------------------------------------------------
# Contributions
# ---------------------------------------------------------------------------
def reference_exists(reference: str) -> bool:
    """Return True if a contribution with this Paystack reference is recorded."""
    if not reference:
        return False
    target = _clean(reference)
    ws = _ws(config.CONTRIBUTIONS_SHEET)
    # Reference is column A; scan that column only.
    for cell in ws.col_values(1)[1:]:
        if _clean(cell) == target:
            return True
    return False


def add_contribution(
    reference: str,
    member_phone: str,
    member_name: str,
    amount: float,
    payment_date: Optional[str] = None,
    cycle_month: Optional[str] = None,
    status: str = CONTRIB_CONFIRMED,
) -> None:
    """Append a contribution row (append-only). Caller must check reference_exists first."""
    payment_date = payment_date or _today_str()
    cycle_month = cycle_month or date.today().strftime("%Y-%m")
    row = [
        _sanitize(reference),
        _sanitize(member_phone),
        _sanitize(member_name),
        _sanitize(amount),
        _sanitize(payment_date),
        _sanitize(cycle_month),
        _sanitize(status),
        _sanitize(_now_str()),
    ]
    _ws(config.CONTRIBUTIONS_SHEET).append_row(row, value_input_option="USER_ENTERED")
    log.info("Logged contribution ref=%s phone=%s amount=%s",
             reference, redact_phone(member_phone), amount)


def get_contributions_by_phone(phone: str) -> list[dict]:
    """Return all contribution records for a member phone (Confirmed and otherwise)."""
    if not phone:
        return []
    target = _clean(phone)
    return [
        _clean_record(rec) for rec in _records(config.CONTRIBUTIONS_SHEET)
        if _clean(rec.get("Member Phone", "")) == target
    ]


def phones_paid_in_cycle(cycle_month: str) -> set[str]:
    """Return the set of member phones with a Confirmed contribution in a cycle."""
    target = _clean(cycle_month)
    paid: set[str] = set()
    for rec in _records(config.CONTRIBUTIONS_SHEET):
        if _clean(rec.get("Cycle Month", "")) != target:
            continue
        if _clean(rec.get("Status", "")).lower() != CONTRIB_CONFIRMED.lower():
            continue
        phone = _clean(rec.get("Member Phone", ""))
        if phone:
            paid.add(phone)
    return paid


def ping() -> bool:
    """Lightweight connectivity check used by startup diagnostics."""
    _get_spreadsheet()
    return True
