# Notifications Subsystem — Pre-Migration Inventory

**Date:** 2026-04-23
**Author:** Claude Code
**Branch:** `feat/resend-email-migration`
**Purpose:** Establish the as-found state of FinalVerify's outbound email
delivery before replacing it with Resend's HTTPS API.

---

## 1. Functions in `app/services/notifications.py`

The module is small — three private helpers and two public sends.

| Symbol | Lines | Visibility | Purpose |
|---|---|---|---|
| `_GMAIL_HOST = "smtp.gmail.com"` | 21 | private | SMTP host constant |
| `_GMAIL_PORT = 587` | 22 | private | SMTP port (STARTTLS) |
| `_get_credentials() -> tuple[str, str] \| None` | 25 | private | Reads `NOTIFY_EMAIL` and `NOTIFY_EMAIL_APP_PASSWORD` from `os.environ`. Returns `None` (and logs a warning) if either is unset; otherwise returns the pair. |
| `_send(notify_email, notify_password, msg)` | 36 | private | Opens an `smtplib.SMTP` to `smtp.gmail.com:587`, calls `starttls()`, logs in, sends a single `MIMEText` message. |
| `send_contact_notification(name, email, subject, message, organization="")` | 43 | **public** | Sends the admin a plain-text notification when someone submits the contact form. To/From: the admin's `NOTIFY_EMAIL`. Reply-To: the user's email. Returns `None`. Failures are logged via `logger.exception` but never propagate. |
| `send_waitlist_notification(email)` | 81 | **public** | Sends the admin a plain-text notification when someone joins the waitlist. To/From: the admin's `NOTIFY_EMAIL`. No Reply-To. Returns `None`. Failures are logged via `logger.exception` but never propagate. |

**Key observation: both functions are admin alerts, not user-facing emails.**
The product currently sends zero email to end users.

## 2. Call sites

`grep -rn` for `send_contact_notification` and `send_waitlist_notification`:

| File | Line | Context |
|---|---|---|
| `app/routes/pages.py` | 1157 | `from app.services.notifications import send_waitlist_notification` (lazy import inside `/waitlist` handler) |
| `app/routes/pages.py` | 1167 | `background_tasks.add_task(send_waitlist_notification, email)` — fire-and-forget |
| `app/routes/pages.py` | 1185 | `from app.services.notifications import send_contact_notification` (lazy import inside `/contact` handler) |
| `app/routes/pages.py` | 1209 | `background_tasks.add_task(send_contact_notification, name, email, subject, message, organization)` |
| `tests/test_app.py` | 5940 | Test imports both functions to assert they no-op when env vars are unset |

Both real callers route through FastAPI `BackgroundTasks`, so the email is
sent *after* the HTTP response is returned. This means any exception raised
inside the send is logged by the task runner but never seen by the user, and
the response is always `200 OK` regardless of email success — by design,
since email delivery is non-blocking for the form submission flow.

## 3. Where SMTP credentials are read

**Important discrepancy with the migration brief.** The brief described
config keys `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`. The
codebase has none of those.

Real config surface:

| Source | Variable | Used at |
|---|---|---|
| `os.environ` | `NOTIFY_EMAIL` | `app/services/notifications.py:26` |
| `os.environ` | `NOTIFY_EMAIL_APP_PASSWORD` | `app/services/notifications.py:27` |
| `docker-compose.yml` | `NOTIFY_EMAIL`, `NOTIFY_EMAIL_APP_PASSWORD` | commented examples in the `aaa-app` service |

These are **not** declared in `app/settings.py` (the pydantic-settings module).
They live entirely in `os.environ` and are read directly by the notifications
module. Nothing else in the app touches them.

There is no `.env.example` entry for them. There is no SMTP-related entry in
`README.md` or `docs/USER_GUIDE.md`.

## 4. Tests that exercise the notification path

| Test | File:Line | What it asserts |
|---|---|---|
| `test_notification_skipped_when_env_vars_absent` | `tests/test_app.py:5935` | With `NOTIFY_EMAIL` and `NOTIFY_EMAIL_APP_PASSWORD` unset, both send functions return without raising. **This test will need to be inverted under the new design** — the new code will raise `NotificationConfigError` if `RESEND_API_KEY` is missing. |
| `test_contact_form_saves_to_db` | `tests/test_app.py:5796` | Hits `POST /contact`; relies on email being a no-op when env vars absent. |
| `test_contact_form_rejects_missing_fields` | `tests/test_app.py:5821` | Form validation; doesn't reach the send path. |
| `test_waitlist_saves_email_to_db` | `tests/test_app.py:5947` | Hits `POST /waitlist`; relies on email being a no-op when env vars absent. |

The two form-saves tests rely on the BackgroundTasks email never raising
synchronously. After the migration, with the lazy-validation pattern, the
`background_tasks.add_task` call will queue a function that raises
`NotificationConfigError` on first invocation if `RESEND_API_KEY` is unset.
That exception is caught by FastAPI's task runner and logged, so the tests
will continue to pass — but the conftest.py needs to provide a dummy
`RESEND_API_KEY` to prevent the new startup-time validation from blowing up.

## 5. Email types currently sent

| Trigger | Recipient | Reply-To | Body type |
|---|---|---|---|
| New contact form submission (`POST /contact`) | Admin (`NOTIFY_EMAIL`) | Submitter's email | Plain text |
| New waitlist signup (`POST /waitlist`) | Admin (`NOTIFY_EMAIL`) | _(none — defaults to From)_ | Plain text |

**Discrepancy with the migration brief:** the brief mentioned
"waitlist confirmation, password reset, admin alert" as candidate types.
Of those, **only admin alerts exist today**. There is no waitlist confirmation
sent to the user, and there is no password-reset flow anywhere in the
codebase (verified by `grep -rn "password.*reset\|password_reset\|forgot.*password"` against `app/` and `tests/` — zero
matches in any context that would suggest user-facing reset functionality).

The `Idempotency-Key` requirement therefore has no immediate user-facing send
to apply to today, but the design will still wire the key per send so future
additions (waitlist confirmation, password reset) inherit it for free.

---

## 6. Deviations from the migration brief — summary

These are the points where the codebase didn't match the brief's
description. Each one is being handled by best judgment per the brief's
final paragraph; they're called out here so the reviewer can sanity-check.

1. **No `SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD` keys exist.**
   The actual env vars are `NOTIFY_EMAIL` (admin recipient) and
   `NOTIFY_EMAIL_APP_PASSWORD` (Gmail app password). The migration will:
   - Drop `NOTIFY_EMAIL_APP_PASSWORD` (no longer needed under HTTPS).
   - Keep `NOTIFY_EMAIL` as the *admin recipient* address, since admin
     alerts still need a destination.
   - Add `RESEND_API_KEY` and `FINALVERIFY_FROM_EMAIL` per the brief.
   - Update the `docker-compose.yml` and `.env.example` accordingly.
   - Flag in `README.md` that `NOTIFY_EMAIL_APP_PASSWORD` is obsolete.

2. **No password-reset flow exists.** The idempotency-key infrastructure
   will be implemented (UUID5 derived from `user_id|recipient + email_type +
   hour bucket`) and applied to every send. No password reset will be added
   in this PR — that's a separate feature.

3. **No user-facing emails exist.** The migration preserves the two
   admin-alert sends and adds one more (admin test email at
   `/admin/test-email`). The Reply-To rule from the brief
   (`john@finalverify.com`) is applied to:
   - The waitlist admin alert (no per-submitter reply-to is meaningful).
   - The new admin test email.
   For the contact-form alert, Reply-To remains the submitter's email so
   the admin can hit Reply and respond directly to the user — this matches
   existing behavior and is the more useful default.

4. **"Fail loudly at startup."** Implemented as:
   - `validate_email_config()` is called during `create_app()`. If
     `RESEND_API_KEY` is unset, it raises `NotificationConfigError` and the
     app refuses to start.
   - First-send call also re-validates as defense in depth.
   - `tests/conftest.py` will set `RESEND_API_KEY="re_test_dummy"` so the
     test suite passes startup validation. Real network calls are mocked.

5. **Public function signatures.** The brief asked these be preserved.
   The new functions return `SendResult` (a small dataclass) instead of
   `None`. Existing callers (`background_tasks.add_task(...)`) discard the
   return value, so they need no changes. The signature of the *parameters*
   is preserved exactly. This deviation is purely additive.

6. **Project uses `pyproject.toml`, not `requirements.txt`.** The Resend SDK
   will be added under `[project.dependencies]` following the existing
   range-pin pattern (`>=X.Y.0,<X+1.0.0`).

---

## 7. Files this migration will touch

Pre-emptive list, written so the reviewer can scan it before code lands:

- `pyproject.toml` — add `resend` dependency
- `app/services/notifications.py` — full rewrite, smtplib removed
- `app/settings.py` — add `resend_api_key`, `finalverify_from_email`,
  `notify_email`
- `app/main.py` — call `validate_email_config()` during startup
- `app/routes/pages.py` — add `POST /admin/test-email` admin route
- `.env.example` — add new vars, remove obsolete
- `docker-compose.yml` — replace `NOTIFY_EMAIL_APP_PASSWORD` block with
  `RESEND_API_KEY` + `FINALVERIFY_FROM_EMAIL`
- `README.md` — short note that `RESEND_API_KEY` is required in production
- `tests/conftest.py` — set `RESEND_API_KEY="re_test_dummy"` and stub the
  `resend.Emails.send` call
- `tests/test_app.py` — invert the env-var-absent test, add new tests for
  config error / from-address default / idempotency-key stability

No changes planned to `aaa_db/`, `alembic/`, the templates, or any
unrelated service module.
