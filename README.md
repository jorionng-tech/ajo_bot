# Ajo / Contribution Tracker

A standalone, self-hostable WhatsApp contribution-management bot for savings
cooperatives, **Ajo** groups, ROSCAs, and investment clubs.
Built by **Jorion Technologies**. Designed to be cloned and deployed by any developer.

> *"Ajo"* is the Nigerian term for a rotating savings / contribution group — the
> primary audience for this bot.

---

## 1. What it does

A complete self-service member onboarding + contribution tracking system. The
whole lifecycle is **JOIN → pay → track**:

- **Member self-onboarding (WhatsApp `JOIN`)** — a prospective member texts
  `JOIN`, sees the single Ajo plan, confirms with `YES`, gives their name and
  email, and receives the Paystack payment link. A **Pending** member row is
  written to Google Sheets.
- **Payment confirmation (Paystack webhook)** — when the member pays, Paystack
  fires `charge.success`. The bot matches them by email, flips them to
  **Active**, logs the contribution, sends a WhatsApp receipt (and an optional
  email), and notifies the secretary on Telegram.
- **Balance self-serve (WhatsApp `BALANCE`)** — a member texts `BALANCE` and gets
  their contribution summary (total, cycles paid, last payment, this month).
- **Monthly reminders (scheduled)** — a few days before the due date, the bot
  WhatsApps Active members who haven't paid this cycle, and sends the secretary a
  Telegram defaulters summary.

This is a **Tier 1** bot: exactly **one** Ajo plan, no loans, no interest, no
multi-group. There is **no Claude / LLM** anywhere — every trigger is a
structured keyword or a signed webhook, so running cost is zero and setup stays
simple. See *Tier 2 upgrade path* below.

---

## 2. Prerequisites

- **Python 3.11+**
- A **Paystack** account (with a payment page / plan link)
- A **Google Cloud** project + service account (for Google Sheets)
- A **Twilio** account with the WhatsApp sandbox (or an approved sender)
- A **Telegram** bot + your admin chat ID
- *(Optional)* a **Brevo** account if you want email receipts
- A way to expose your local server over **HTTPS** (e.g. `ngrok`) during testing

---

## 3. Google Sheets + Service Account setup (do this carefully)

The Google Sheet **is the database**. There is no other store.

1. Go to the [Google Cloud Console](https://console.cloud.google.com/), create a
   project (or pick one).
2. **Enable APIs**: enable both the **Google Sheets API** and the
   **Google Drive API** for that project.
3. **Create a Service Account**: *IAM & Admin → Service Accounts → Create*.
   Give it a name; no special roles are required.
4. **Create a key**: open the service account → *Keys → Add key → Create new key
   → JSON*. A JSON file downloads.
5. **Save the key as `credentials.json`** in the project root (or set
   `GOOGLE_CREDENTIALS_FILE` to its path).
   - ⚠️ **`credentials.json` contains a private key. Treat it like a password.
     It is already in `.gitignore` — never commit it, never paste it anywhere.**
6. **Share the spreadsheet** with the service account's email address
   (looks like `name@project.iam.gserviceaccount.com`) as an **Editor**.
   This step is mandatory — without it the bot gets a 403.
7. **Get the Spreadsheet ID** from the sheet URL:
   `https://docs.google.com/spreadsheets/d/`**`<THIS_IS_THE_ID>`**`/edit`
   Put it in `.env` as `GOOGLE_SPREADSHEET_ID`.

---

## 4. Spreadsheet setup (exact tabs + headers)

Create **one** spreadsheet with **two tabs**, named exactly (case-sensitive):

### Tab `Members`
Header row (row 1), columns A–H, exactly:

| A | B | C | D | E | F | G | H |
|---|---|---|---|---|---|---|---|
| Name | Phone | Email | Status | Plan | Monthly Amount | Join Date | Notes |

- **Phone** is E.164 (`+2348012345678`) and is the member primary key.
- **Status** is one of `Pending` / `Active` / `Inactive`.

### Tab `Contributions`
Header row (row 1), columns A–H, exactly:

| A | B | C | D | E | F | G | H |
|---|---|---|---|---|---|---|---|
| Reference | Member Phone | Member Name | Amount | Payment Date | Cycle Month | Status | Logged At |

- **Reference** is the Paystack reference and must be unique (idempotency key).
- **Cycle Month** is `YYYY-MM`.

> Headers must match **exactly** (the bot reads rows by header name).

---

## 5. Paystack setup

1. Create your plan / **payment page** in the Paystack dashboard and copy its
   link. Put it in `ajo_config.json` under `plan.paystack_payment_link`.
   (Tier 1 uses this **static** link; it does not generate links via the API.)
2. In *Settings → API Keys & Webhooks*:
   - Copy your **secret key** → `.env` `PAYSTACK_SECRET_KEY`.
   - Set the **Webhook URL** to your public `…/paystack/webhook`.
   - Set a **webhook secret** → `.env` `PAYSTACK_WEBHOOK_SECRET`. The bot
     validates `X-Paystack-Signature` (HMAC-SHA512) against this and rejects
     mismatches with `401`.
3. Make sure the **`charge.success`** event is enabled.
4. **Metadata recommendation:** where you can, include the member's `phone`
   (E.164) and `name` in the payment metadata. The bot uses email to match, but
   metadata phone lets it send a WhatsApp receipt even when someone pays without
   going through the `JOIN` flow first.

---

## 6. Twilio WhatsApp sandbox setup

1. In the Twilio console open *Messaging → Try it out → WhatsApp sandbox*.
2. Join the sandbox from your phone (send the given `join …` code to the sandbox
   number).
3. Copy **Account SID** and **Auth Token** → `.env`
   (`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`).
4. Set `TWILIO_WHATSAPP_FROM` to the sandbox number, e.g.
   `whatsapp:+14155238886`.
5. Set the sandbox **"When a message comes in"** webhook to your public
   `…/whatsapp/webhook` (HTTP POST).
6. Set `PUBLIC_WEBHOOK_URL` in `.env` to that **exact** public URL — the bot
   validates Twilio's `X-Twilio-Signature` against it (rejects `403` on
   mismatch; there is **no** sandbox bypass).

---

## 7. Telegram bot setup

1. Message **@BotFather**, send `/newbot`, follow the prompts, copy the token →
   `.env` `TELEGRAM_BOT_TOKEN`.
2. Start a chat with your new bot (send it any message).
3. Get your **chat ID** (e.g. message **@userinfobot**, or read
   `https://api.telegram.org/bot<token>/getUpdates`) → `.env`
   `TELEGRAM_ADMIN_CHAT_ID`.

---

## 8. Configuration

1. Copy `.env.example` to `.env` and fill in every value (see Section 4 of the
   build spec for the full list). **Never commit `.env`.**
2. Edit `ajo_config.json` for your group:

```json
{
  "group_name": "Ikoyi Professionals Ajo",
  "currency": "NGN",
  "plan": {
    "name": "Monthly Ajo",
    "monthly_amount": 50000,
    "paystack_payment_link": "https://paystack.com/pay/your-plan-link"
  },
  "monthly_due_day": 25,
  "reminder_days_before": [5, 2],
  "timezone": "Africa/Lagos",
  "join_keyword": "JOIN",
  "balance_keyword": "BALANCE",
  "send_email_receipt": false,
  "admin_name": "Secretary"
}
```

- Tier 1 has exactly **one** plan.
- `join_keyword` / `balance_keyword` are matched case-insensitively.
- Set `send_email_receipt` to `true` only if you've configured Brevo.

You can check your configuration at any time:

```bash
python config.py        # prints anything still missing
```

---

## 9. Run

```bash
python -m venv .venv && source .venv/bin/activate   # optional but recommended
pip install -r requirements.txt
python app.py
```

The app listens on `PORT` (default `5002`). Expose it over **HTTPS** so Twilio
and Paystack can reach it, e.g.:

```bash
ngrok http 5002
```

Then point the Twilio and Paystack webhook URLs at the public HTTPS address
(and update `PUBLIC_WEBHOOK_URL` to match the WhatsApp endpoint).

Health check: `GET /health`.

---

## 10. Reminders (scheduled)

`reminders.py` sends monthly reminders. By default it only acts when today is one
of `reminder_days_before` days before `monthly_due_day`, so it's safe to run
daily.

```bash
python reminders.py            # send if today is a reminder day
python reminders.py --force    # send now regardless of the date
python reminders.py --dry-run  # preview only, send nothing
```

Schedule it with **cron** (Linux/macOS) or **Task Scheduler** (Windows), e.g.
run daily at 09:00 Africa/Lagos:

```cron
0 9 * * *  cd /path/to/ajo-contribution-tracker && /path/to/.venv/bin/python reminders.py >> logs/cron.log 2>&1
```

---

## 11. Security notes

- **`credentials.json`** (Google service account key) and **`.env`** contain
  secrets. Both are in `.gitignore` — **never commit them**. Treat them like
  passwords.
- Webhook signatures are verified: Paystack (`HMAC-SHA512`, `401` on mismatch)
  and Twilio (`RequestValidator` against `PUBLIC_WEBHOOK_URL`, `403` on
  mismatch).
- Idempotency: Paystack references are de-duplicated against the Contributions
  sheet; Twilio `MessageSid`s are tracked in memory.
- Inbound WhatsApp is rate-limited per number (~10/min).
- Logs **redact phone numbers** and never contain names or secrets.
- Run with `FLASK_ENV=production` (the default) so Flask `debug` is **off** and
  stack traces are never returned to callers.

---

## 12. Tier 2 upgrade path

Available from **Jorion Technologies**:

- Multiple Ajo plans
- Loan disbursement & tracking
- Interest / dividend calculation
- Multiple Ajo groups in one deployment
- Web dashboard / admin UI
- Persistent conversation state (Redis) across restarts

---

## License

MIT — see [LICENSE](LICENSE). © 2026 Jorion Technologies Limited.
