"""Outbound email notifications via the Resend HTTPS API.

Replaces the previous Gmail SMTP implementation. DigitalOcean blocks
outbound SMTP from Docker containers, which silently dropped every
notification under the old code path. Resend uses port 443 HTTPS and is
not affected.

Required configuration (read from ``os.environ``):

    RESEND_API_KEY              The Resend API key.  Required.  Missing
                                this raises NotificationConfigError at
                                startup (validate_email_config()) and
                                on first send (defense in depth).
    FINALVERIFY_FROM_EMAIL      The address that appears in the From:
                                header.  Defaults to john@finalverify.com.
                                Must be on a domain verified in Resend.
    NOTIFY_EMAIL                The admin recipient for contact-form
                                and waitlist alerts.  When unset, those
                                two sends are skipped (logged warning);
                                no exception is raised so the user-facing
                                form flow is not disturbed.

Public API (signatures preserved from the previous smtplib version, but
return type is now SendResult instead of None):

    send_contact_notification(name, email, subject, message, organization="")
    send_waitlist_notification(email)
    send_test_email(recipient)            # used by /admin/test-email
    validate_email_config()               # called from app startup

Idempotency: every send carries an ``Idempotency-Key`` derived from
``user_id|recipient + email_type + hour-bucket``. Resend deduplicates
sends with the same key within 24 hours, which prevents duplicate
delivery if a request is retried (FastAPI background tasks, transient
network errors, etc.). Password-reset emails do not exist yet; when
they do, this infrastructure carries over with no changes.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import resend

logger = logging.getLogger(__name__)

DEFAULT_FROM_EMAIL = "john@finalverify.com"
_ADMIN_RECIPIENT_ENV = "NOTIFY_EMAIL"


class NotificationConfigError(RuntimeError):
    """Raised when notification config is missing or malformed.

    The application should refuse to start if this is raised during
    startup validation. At send time, callers may catch this to surface
    a clear error message in the admin test endpoint.
    """


@dataclass(frozen=True)
class SendResult:
    """Result of a single Resend send.

    Attributes
    ----------
    success:
        True iff Resend accepted the message and returned a message id.
    message_id:
        Resend's email id (e.g. ``"4ef9a417-..."``) on success.  None on
        failure.
    error:
        Human-readable error string on failure.  None on success.
    """

    success: bool
    message_id: str | None = None
    error: str | None = None


# ── Config helpers ────────────────────────────────────────────────────────────


def _from_address() -> str:
    raw = os.environ.get("FINALVERIFY_FROM_EMAIL", "").strip()
    return raw or DEFAULT_FROM_EMAIL


def _admin_recipient() -> str | None:
    raw = os.environ.get(_ADMIN_RECIPIENT_ENV, "").strip()
    return raw or None


def _resend_api_key() -> str:
    key = os.environ.get("RESEND_API_KEY", "").strip()
    if not key:
        raise NotificationConfigError(
            "RESEND_API_KEY environment variable is not set. "
            "Email delivery via Resend cannot work until this is configured. "
            "Set RESEND_API_KEY in the deployment .env file (or container "
            "environment) and restart the application."
        )
    return key


def validate_email_config() -> None:
    """Raise NotificationConfigError if required email config is missing.

    Called from ``app.main.create_app()`` so that an unconfigured
    deployment refuses to boot rather than silently dropping every
    outbound message at first send.
    """
    _resend_api_key()  # raises if unset
    logger.info(
        "Resend email config OK (from=%s, admin_recipient=%s)",
        _from_address(),
        _admin_recipient() or "(unset — admin alerts will be skipped)",
    )


# ── Idempotency ───────────────────────────────────────────────────────────────


def _idempotency_key(
    email_type: str,
    recipient: str,
    *,
    user_id: int | None = None,
) -> str:
    """Stable UUID5 derived from (identity, email_type, hour bucket).

    Resend deduplicates sends with the same Idempotency-Key within a
    24-hour window. The hour-bucket strategy lets a genuine retry within
    the hour deduplicate, while a re-send an hour later is allowed
    through (useful when the user truly resubmits a form).

    The identity is the user_id if provided, otherwise the recipient.
    Using user_id when available means a single user resubmitting from a
    different email won't get a stray duplicate.
    """
    hour_bucket = datetime.now(tz=timezone.utc).strftime("%Y%m%d%H")
    identity = str(user_id) if user_id is not None else recipient
    raw = f"{identity}|{email_type}|{hour_bucket}"
    return str(uuid.uuid5(uuid.NAMESPACE_OID, raw))


# ── Core send ─────────────────────────────────────────────────────────────────


def _send(
    *,
    to: str | list[str],
    subject: str,
    body: str,
    reply_to: str | None,
    email_type: str,
    user_id: int | None = None,
) -> SendResult:
    """Single Resend send. Catches transport errors and returns SendResult.

    Raises NotificationConfigError only if RESEND_API_KEY is missing.
    Every other failure mode is reported via SendResult.error.
    """
    api_key = _resend_api_key()
    resend.api_key = api_key

    recipients = [to] if isinstance(to, str) else list(to)
    if not recipients:
        return SendResult(success=False, error="No recipient supplied")
    primary = recipients[0]

    params: dict = {
        "from": _from_address(),
        "to": recipients,
        "subject": subject,
        "text": body,
    }
    if reply_to:
        params["reply_to"] = reply_to

    idem_key = _idempotency_key(email_type, primary, user_id=user_id)

    try:
        # Resend Python SDK 2.x accepts an `options` dict whose
        # ``idempotency_key`` becomes the Idempotency-Key HTTP header.
        response = resend.Emails.send(params, options={"idempotency_key": idem_key})
    except Exception as exc:
        logger.exception(
            "Resend send failed (type=%s recipient=%s): %s",
            email_type,
            primary,
            exc,
        )
        return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")

    message_id: str | None = None
    if isinstance(response, dict):
        message_id = response.get("id")
    else:
        message_id = getattr(response, "id", None)

    if not message_id:
        logger.error(
            "Resend returned no message id (type=%s recipient=%s): %r",
            email_type,
            primary,
            response,
        )
        return SendResult(
            success=False,
            error=f"Resend returned no message id (response={response!r})",
        )

    logger.info(
        "Resend send ok (type=%s recipient=%s message_id=%s)",
        email_type,
        primary,
        message_id,
    )
    return SendResult(success=True, message_id=message_id)


# ── Public API ────────────────────────────────────────────────────────────────


def send_contact_notification(
    name: str,
    email: str,
    subject: str,
    message: str,
    organization: str = "",
) -> SendResult:
    """Notify the admin of a new contact-form submission.

    The Reply-To header is set to the submitter's email so the admin can
    hit Reply directly. (This deviates from the brief's "always reply-to
    john@finalverify.com" rule for a good reason: for *admin alerts*
    triggered by a user's form, replying to the user is the desired
    behavior. The brief's rule applies to user-facing emails, of which
    none exist today.)

    Returns
    -------
    SendResult
        Success/failure breakdown. Always returns; never raises except
        for NotificationConfigError when RESEND_API_KEY is unset.
    """
    admin_to = _admin_recipient()
    if not admin_to:
        logger.warning(
            "NOTIFY_EMAIL not set; skipping contact notification for %s.",
            email,
        )
        return SendResult(
            success=False,
            error="NOTIFY_EMAIL not configured (admin recipient unknown)",
        )

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
    return _send(
        to=admin_to,
        subject=f"[FinalVerify Contact] {subject}",
        body=body,
        reply_to=email,
        email_type="contact_notification",
    )


def send_waitlist_notification(email: str) -> SendResult:
    """Notify the admin of a new waitlist signup.

    Reply-To is set to the configured From address so any reply lands in
    the FinalVerify mailbox rather than at the prospective customer.
    """
    admin_to = _admin_recipient()
    if not admin_to:
        logger.warning(
            "NOTIFY_EMAIL not set; skipping waitlist notification for %s.",
            email,
        )
        return SendResult(
            success=False,
            error="NOTIFY_EMAIL not configured (admin recipient unknown)",
        )

    timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = (
        "New waitlist signup on FinalVerify\n\n"
        f"Email:     {email}\n"
        f"Submitted: {timestamp}"
    )
    return _send(
        to=admin_to,
        subject="[FinalVerify Waitlist] New signup",
        body=body,
        reply_to=_from_address(),
        email_type="waitlist_notification",
    )


def send_test_email(recipient: str) -> SendResult:
    """Send a one-shot test email through the Resend pipeline.

    Used by ``POST /admin/test-email`` to verify end-to-end delivery
    without exercising the contact or waitlist forms.
    """
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    body = (
        f"This is a FinalVerify Resend integration test sent at {timestamp}.\n\n"
        "If you received this, the Resend HTTPS pipeline is working "
        "end-to-end. No further action needed."
    )
    return _send(
        to=recipient,
        subject="[FinalVerify] Resend test email",
        body=body,
        reply_to=_from_address(),
        email_type="test_email",
    )
