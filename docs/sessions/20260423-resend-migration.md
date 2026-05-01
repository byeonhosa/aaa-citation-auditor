# Resend Email Migration — Session Notes

**Date:** 2026-04-23
**Branch:** `feat/resend-email-migration`
**Author:** Claude Code

## Summary

Replaced FinalVerify's outbound email path. Old: `smtplib` against
`smtp.gmail.com:587` with a Gmail App Password. New: Resend's HTTPS API
via the official Python SDK. DigitalOcean blocks outbound SMTP from
Docker containers, which had been silently dropping every contact-form
and waitlist alert. Resend uses port 443 and is unaffected.

The product has no user-facing email today (only admin alerts), so the
migration is entirely about the `notifications.py` service and its two
existing call sites in the contact and waitlist routes — plus a new
`/admin/test-email` endpoint for one-shot verification post-deploy.

Six commits, one per task in the brief plus the inventory commit:

1. `a25e16b` — Inventory current SMTP notification subsystem
2. `1b73925` — Add `resend>=2.5.0,<3.0.0` dependency
3. `862bf7b` — Replace `smtplib` with Resend HTTPS API in notifications service
4. `01bfaa3` — Wire Resend config into settings, startup, and deployment docs
5. `c261517` — Update notification tests for Resend; add new coverage
6. `7cd0651` — Add `POST /admin/test-email` for end-to-end Resend verification

Task 7 ("remove dead SMTP code") is folded into commit `862bf7b` — the
rewrite did not preserve a fallback path. `grep -rn smtplib` confirms no
remaining code-level references; only documentary mentions remain.

## Files modified

- `pyproject.toml` — adds `resend>=2.5.0,<3.0.0`
- `app/services/notifications.py` — full rewrite (288 lines added,
  56 removed). Public function names and parameters preserved; return
  type changed from `None` to `SendResult` (additive change — existing
  `background_tasks.add_task(...)` callers discard the return value).
- `app/settings.py` — three new pydantic fields: `resend_api_key`,
  `finalverify_from_email`, `notify_email`
- `app/main.py` — calls `validate_email_config()` during `create_app()`;
  app refuses to boot when `RESEND_API_KEY` is unset
- `app/routes/pages.py` — new `POST /admin/test-email` endpoint
- `.env.example` — three new vars; flags `NOTIFY_EMAIL_APP_PASSWORD`
  obsolete
- `docker-compose.yml` — replaces the Gmail App Password block with the
  Resend equivalent
- `README.md` — three new rows in the Configuration Reference table
- `tests/conftest.py` — sets dummy `RESEND_API_KEY` / `FINALVERIFY_FROM_EMAIL`
  / `NOTIFY_EMAIL` env vars; autouse fixture stubs
  `resend.Emails.send` so background-task sends in form tests don't hit
  the real Resend API
- `tests/test_app.py` — retires the obsolete smtplib-era no-op test;
  replaces it with a SendResult shape check for the
  `NOTIFY_EMAIL`-unset case
- `tests/test_notifications.py` (new) — 17 unit tests covering config
  validation, From: defaults, idempotency-key stability, SendResult
  shapes, Reply-To routing, admin-recipient handling, and the
  `/admin/test-email` endpoint
- `docs/notifications-inventory-20260423.md` (new) — inventory committed
  before any code changed

## Tests to run on the droplet

```bash
# Full suite — should pass at the same count as before plus 17 new tests
# from test_notifications.py
pytest -q

# Targeted: the new and modified files
pytest -q tests/test_notifications.py tests/test_app.py::test_notification_returns_failure_result_when_notify_email_absent

# Lint check (project policy: 0 new warnings)
ruff check .
```

If any test fails, the most likely culprits are:

- The Resend SDK 2.x `options={"idempotency_key": ...}` parameter
  shape — older 2.x versions may want a different keyword. If
  `test_idempotency_key_*` fails with a `TypeError`, check the installed
  SDK version (`pip show resend`) and adjust the call site in
  `app/services/notifications.py:_send` (one place).
- `resend.Emails.send` import path. If the SDK exposes
  `resend.send_emails` instead of `resend.Emails.send` in the version
  installed on the droplet, the test stubs in `conftest.py` and
  `test_notifications.py` need to follow.

## Deployment instructions

```bash
# On the droplet:
cd /opt/finalverify  # or wherever the working copy lives
git fetch origin
git checkout feat/resend-email-migration

# 1. Set the Resend key in the deployment .env (NOT committed):
#    Add this line to /opt/finalverify/.env (or the production .env path):
#    RESEND_API_KEY=re_xxx_your_real_key_here
#    FINALVERIFY_FROM_EMAIL=john@finalverify.com
#    NOTIFY_EMAIL=john@finalverify.com   # or whichever admin inbox
#
# 2. (Optional) remove the obsolete entry:
#    NOTIFY_EMAIL_APP_PASSWORD=...

# 3. Rebuild and restart:
docker compose build aaa-app
docker compose up -d aaa-app

# 4. Verify the app started cleanly (no NotificationConfigError):
docker compose logs --tail 50 aaa-app
# Expected line: "Resend email config OK (from=john@finalverify.com, ...)"

# 5. End-to-end test:
#    Log in as the admin user (user_id==1), then from a curl session
#    with the admin session cookie, or via the browser dev tools:
curl -X POST https://finalverify.com/admin/test-email \
  -H "Cookie: session=<admin-session-cookie>" \
  -d "recipient=qa-account@yourbox.com"
# Expected: {"ok": true, "message_id": "..."}
# Then confirm the test email arrived at qa-account@yourbox.com.
```

## Rollback instructions

If `/admin/test-email` returns failure or contact-form alerts stop
delivering after deploy:

```bash
# 1. Quick rollback — point the deployment back at main without losing
# the work:
cd /opt/finalverify
git checkout main
docker compose build aaa-app
docker compose up -d aaa-app
# This restores the smtplib code path.  Outbound email will return
# to its pre-migration state (i.e. silently dropped, since DigitalOcean
# still blocks SMTP) — but the app will be back to its known state.

# 2. Diagnose without rolling back: hit /admin/test-email and inspect
# the response body — the {"error": "..."} field carries the Resend
# transport error verbatim.  Common causes:
#   - "401 Unauthorized": RESEND_API_KEY is wrong or revoked.
#   - "domain ... not verified": finalverify.com is not yet verified
#     in the Resend dashboard, or FINALVERIFY_FROM_EMAIL points at a
#     different domain.
#   - "no message id" in the error: SDK / API version mismatch — see
#     the "Tests to run" section above for the likely culprits.

# 3. If a partial rollback is needed (keep the new admin endpoint but
# revert the contact/waitlist behavior), do not — the legacy code path
# is gone, and adding back smtplib for one route is a worse position
# than just fixing the Resend config.
```

## Open items not in this PR

- The Obsidian-vault `Current-Context.md` snapshot is out of date for
  reasons unrelated to this PR (see `FinalVerify_Status_Audit_20260422.md`
  in the repo root). I'm updating that file as part of the session
  handoff but it's not part of the diff.
- The unrelated audit file `FinalVerify_Status_Audit_20260422.md` is
  left untracked in the working copy — it's from a prior session and
  isn't part of this migration.

## Reviewer checklist

- [ ] Resend API key is set in the production `.env` *before* the new
      image is rolled out (the app will refuse to start otherwise).
- [ ] `finalverify.com` is verified as a sending domain in the Resend
      dashboard for `john@finalverify.com`.
- [ ] `NOTIFY_EMAIL` is set to the admin inbox you actually monitor.
- [ ] After deploy, `POST /admin/test-email` returns 200 and the test
      email arrives.
- [ ] Submit a real contact-form entry and confirm the admin alert
      lands within ~1 minute.
