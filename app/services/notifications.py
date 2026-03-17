"""Fire-and-forget email notifications via Gmail SMTP.

Credentials are read from environment variables only:
  NOTIFY_EMAIL              — Gmail address used to send (and receive) notifications
  NOTIFY_EMAIL_APP_PASSWORD — Gmail app password (not the account password)

If either variable is unset, notifications are skipped silently.
Sending failures are logged but never propagate to callers.
"""

from __future__ import annotations

import logging
import os
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

_GMAIL_HOST = "smtp.gmail.com"
_GMAIL_PORT = 587


def _get_credentials() -> tuple[str, str] | None:
    email = os.environ.get("NOTIFY_EMAIL", "").strip()
    password = os.environ.get("NOTIFY_EMAIL_APP_PASSWORD", "").strip()
    if not email or not password:
        logger.warning(
            "NOTIFY_EMAIL or NOTIFY_EMAIL_APP_PASSWORD not set; "
            "skipping email notification."
        )
        return None
    return email, password


def _send(notify_email: str, notify_password: str, msg: MIMEText) -> None:
    with smtplib.SMTP(_GMAIL_HOST, _GMAIL_PORT) as server:
        server.starttls()
        server.login(notify_email, notify_password)
        server.send_message(msg)


def send_contact_notification(
    name: str,
    email: str,
    subject: str,
    message: str,
    organization: str = "",
) -> None:
    """Send admin notification for a new contact form submission."""
    creds = _get_credentials()
    if creds is None:
        return
    notify_email, notify_password = creds

    timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = (
        "New contact form submission on FinalVerify\n\n"
        f"Name:         {name}\n"
        f"Organization: {organization or '(not provided)'}\n"
        f"Email:        {email}\n"
        f"Subject:      {subject}\n"
        f"Submitted:    {timestamp}\n\n"
        f"Message:\n{message}\n\n"
        f"---\nReply directly to {email} to respond."
    )

    msg = MIMEText(body)
    msg["Subject"] = f"[FinalVerify Contact] {subject}"
    msg["From"] = notify_email
    msg["To"] = notify_email
    msg["Reply-To"] = email

    try:
        _send(notify_email, notify_password, msg)
        logger.info("Contact notification sent for: %s", email)
    except Exception:
        logger.exception("Failed to send contact notification email.")


def send_waitlist_notification(email: str) -> None:
    """Send admin notification for a new waitlist signup."""
    creds = _get_credentials()
    if creds is None:
        return
    notify_email, notify_password = creds

    timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = (
        "New waitlist signup on FinalVerify\n\n"
        f"Email:     {email}\n"
        f"Submitted: {timestamp}"
    )

    msg = MIMEText(body)
    msg["Subject"] = "[FinalVerify Waitlist] New signup"
    msg["From"] = notify_email
    msg["To"] = notify_email

    try:
        _send(notify_email, notify_password, msg)
        logger.info("Waitlist notification sent for: %s", email)
    except Exception:
        logger.exception("Failed to send waitlist notification email.")
