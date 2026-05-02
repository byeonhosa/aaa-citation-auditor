"""Unit tests for the Resend-based notifications service.

These tests do not hit the Resend network. They monkeypatch
``resend.Emails.send`` with a stub that records the call and returns a
canned message id.

The old smtplib-era tests in test_app.py have been retired; see
test_notification_resend_path_replaces_legacy below for the inverted
``env vars unset`` behavior check.
"""

from __future__ import annotations

import re
from typing import Any

import pytest


# ── Helpers ──────────────────────────────────────────────────────────────────


class _SendRecorder:
    """Captures every resend.Emails.send call so tests can assert on params."""

    def __init__(self, message_id: str = "test-message-id-001") -> None:
        self.calls: list[dict[str, Any]] = []
        self.message_id = message_id

    def __call__(self, params: dict, options: dict | None = None) -> dict:
        self.calls.append({"params": params, "options": options or {}})
        return {"id": self.message_id}


@pytest.fixture()
def recorder(monkeypatch: pytest.MonkeyPatch) -> _SendRecorder:
    """Patches resend.Emails.send with a recording stub."""
    rec = _SendRecorder()
    import resend

    monkeypatch.setattr(resend.Emails, "send", rec)
    return rec


# ── Config validation ────────────────────────────────────────────────────────


def test_validate_email_config_passes_when_resend_api_key_present() -> None:
    """Smoke-check: conftest sets RESEND_API_KEY, so validation should pass."""
    from app.services.notifications import validate_email_config

    # Should not raise.
    validate_email_config()


def test_validate_email_config_raises_when_resend_api_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing RESEND_API_KEY must raise NotificationConfigError, not be silent."""
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    from app.services.notifications import (
        NotificationConfigError,
        validate_email_config,
    )

    with pytest.raises(NotificationConfigError) as exc_info:
        validate_email_config()
    # Error message should mention the offending var so ops can fix it.
    assert "RESEND_API_KEY" in str(exc_info.value)


def test_first_send_raises_when_resend_api_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense in depth: even if startup somehow skipped, first send raises."""
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    from app.services.notifications import (
        NotificationConfigError,
        send_waitlist_notification,
    )

    with pytest.raises(NotificationConfigError):
        send_waitlist_notification("user@example.com")


# ── From address ─────────────────────────────────────────────────────────────


def test_from_address_defaults_to_john_finalverify(
    monkeypatch: pytest.MonkeyPatch, recorder: _SendRecorder
) -> None:
    """When FINALVERIFY_FROM_EMAIL is unset, From: defaults to john@finalverify.com."""
    monkeypatch.delenv("FINALVERIFY_FROM_EMAIL", raising=False)
    from app.services.notifications import send_test_email

    result = send_test_email("recipient@example.com")
    assert result.success
    assert recorder.calls[0]["params"]["from"] == "john@finalverify.com"


def test_from_address_uses_env_override(
    monkeypatch: pytest.MonkeyPatch, recorder: _SendRecorder
) -> None:
    """FINALVERIFY_FROM_EMAIL env var overrides the default."""
    monkeypatch.setenv("FINALVERIFY_FROM_EMAIL", "alerts@finalverify.com")
    from app.services.notifications import send_test_email

    send_test_email("recipient@example.com")
    assert recorder.calls[0]["params"]["from"] == "alerts@finalverify.com"


# ── Idempotency keys ─────────────────────────────────────────────────────────


def test_idempotency_key_is_stable_for_same_logical_send(
    monkeypatch: pytest.MonkeyPatch, recorder: _SendRecorder
) -> None:
    """Two sends with identical (recipient, email_type, hour) must produce
    the same Idempotency-Key so Resend deduplicates retries."""
    from app.services.notifications import send_waitlist_notification

    send_waitlist_notification("dedupe-test@example.com")
    send_waitlist_notification("dedupe-test@example.com")

    assert len(recorder.calls) == 2
    key_1 = recorder.calls[0]["options"]["idempotency_key"]
    key_2 = recorder.calls[1]["options"]["idempotency_key"]
    assert key_1 == key_2, "Idempotency keys must match for retries"
    # Stable UUID5 — should match the canonical UUID format.
    assert re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", key_1)


def test_idempotency_key_differs_across_email_types(
    monkeypatch: pytest.MonkeyPatch, recorder: _SendRecorder
) -> None:
    """Sending different email types to the same recipient must produce
    distinct keys — otherwise a contact send could deduplicate against an
    earlier waitlist send."""
    from app.services.notifications import (
        send_contact_notification,
        send_waitlist_notification,
    )

    send_waitlist_notification("collision@example.com")
    send_contact_notification("Test", "collision@example.com", "S", "M")
    assert len(recorder.calls) == 2
    key_waitlist = recorder.calls[0]["options"]["idempotency_key"]
    key_contact = recorder.calls[1]["options"]["idempotency_key"]
    assert key_waitlist != key_contact


def test_idempotency_key_differs_across_recipients(
    monkeypatch: pytest.MonkeyPatch, recorder: _SendRecorder
) -> None:
    """Different recipients must produce distinct keys for the same email type."""
    from app.services.notifications import send_test_email

    send_test_email("a@example.com")
    send_test_email("b@example.com")
    assert recorder.calls[0]["options"]["idempotency_key"] != (
        recorder.calls[1]["options"]["idempotency_key"]
    )


# ── Send-result shape ────────────────────────────────────────────────────────


def test_send_result_success_carries_message_id(recorder: _SendRecorder) -> None:
    """Successful sends return SendResult(success=True, message_id=...)."""
    from app.services.notifications import send_test_email

    result = send_test_email("ok@example.com")
    assert result.success is True
    assert result.message_id == "test-message-id-001"
    assert result.error is None


def test_send_result_failure_when_resend_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resend transport errors return SendResult(success=False, error=...)
    rather than raising."""
    import resend

    def _boom(params: dict, options: dict | None = None) -> dict:
        raise RuntimeError("simulated network failure")

    monkeypatch.setattr(resend.Emails, "send", _boom)
    from app.services.notifications import send_test_email

    result = send_test_email("err@example.com")
    assert result.success is False
    assert result.message_id is None
    assert "simulated network failure" in (result.error or "")


def test_send_result_failure_when_resend_returns_no_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Resend response without an id is treated as failure."""
    import resend

    monkeypatch.setattr(resend.Emails, "send", lambda params, options=None: {})
    from app.services.notifications import send_test_email

    result = send_test_email("noid@example.com")
    assert result.success is False
    assert "no message id" in (result.error or "").lower()


# ── Reply-to routing ─────────────────────────────────────────────────────────


def test_contact_notification_reply_to_is_submitter(recorder: _SendRecorder) -> None:
    """Admin replying to a contact alert should reach the original submitter."""
    from app.services.notifications import send_contact_notification

    send_contact_notification("Jane", "jane@law.example", "Hi", "Body")
    assert recorder.calls[0]["params"]["reply_to"] == "jane@law.example"


def test_waitlist_notification_reply_to_is_from_address(
    monkeypatch: pytest.MonkeyPatch, recorder: _SendRecorder
) -> None:
    """Reply on a waitlist alert should land at the FinalVerify mailbox."""
    monkeypatch.setenv("FINALVERIFY_FROM_EMAIL", "alerts@finalverify.com")
    from app.services.notifications import send_waitlist_notification

    send_waitlist_notification("prospect@example.com")
    assert recorder.calls[0]["params"]["reply_to"] == "alerts@finalverify.com"


def test_test_email_reply_to_is_from_address(
    monkeypatch: pytest.MonkeyPatch, recorder: _SendRecorder
) -> None:
    monkeypatch.setenv("FINALVERIFY_FROM_EMAIL", "alerts@finalverify.com")
    from app.services.notifications import send_test_email

    send_test_email("reviewer@example.com")
    assert recorder.calls[0]["params"]["reply_to"] == "alerts@finalverify.com"


# ── Admin recipient handling ─────────────────────────────────────────────────


def test_contact_notification_skipped_when_notify_email_unset(
    monkeypatch: pytest.MonkeyPatch, recorder: _SendRecorder
) -> None:
    """No NOTIFY_EMAIL means we have no admin to alert.  Skip silently;
    don't raise (the user-facing form must still respond 200)."""
    monkeypatch.delenv("NOTIFY_EMAIL", raising=False)
    from app.services.notifications import send_contact_notification

    result = send_contact_notification("X", "x@y.com", "S", "M")
    assert result.success is False
    assert "NOTIFY_EMAIL" in (result.error or "")
    assert recorder.calls == []  # never reached the Resend stub


def test_waitlist_notification_skipped_when_notify_email_unset(
    monkeypatch: pytest.MonkeyPatch, recorder: _SendRecorder
) -> None:
    monkeypatch.delenv("NOTIFY_EMAIL", raising=False)
    from app.services.notifications import send_waitlist_notification

    result = send_waitlist_notification("x@y.com")
    assert result.success is False
    assert "NOTIFY_EMAIL" in (result.error or "")
    assert recorder.calls == []


# ── /admin/test-email endpoint ───────────────────────────────────────────────


def test_admin_test_email_endpoint_returns_message_id(
    monkeypatch: pytest.MonkeyPatch, recorder: _SendRecorder
) -> None:
    """Admin can POST /admin/test-email with a recipient and get the
    Resend message id back, end-to-end."""
    from fastapi.testclient import TestClient

    from aaa_db.models import User
    from aaa_db.session import SessionLocal
    from app.main import app
    from app.services.auth import hash_password

    # Ensure user_id==1 exists (admin convention used elsewhere).
    with SessionLocal() as db:
        u = db.get(User, 1)
        if u is None:
            u = User(
                id=1,
                email="admin@example.com",
                password_hash=hash_password("adminpass"),
                name="Admin",
            )
            db.add(u)
            db.commit()
            db.refresh(u)
        admin_email = u.email

    client = TestClient(app)
    client.post(
        "/login",
        data={"email": admin_email, "password": "adminpass"},
        follow_redirects=False,
    )

    resp = client.post("/admin/test-email", data={"recipient": "qa@example.com"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["message_id"] == "test-message-id-001"
    assert recorder.calls[0]["params"]["to"] == ["qa@example.com"]


def test_admin_test_email_rejects_non_admin() -> None:
    """Non-admin users get 404 (same pattern as /admin/messages)."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    resp = client.post("/admin/test-email", data={"recipient": "qa@example.com"})
    assert resp.status_code == 404


def test_admin_test_email_rejects_invalid_address(recorder: _SendRecorder) -> None:
    """Bad recipient must not trigger a Resend call."""
    from fastapi.testclient import TestClient

    from aaa_db.models import User
    from aaa_db.session import SessionLocal
    from app.main import app
    from app.services.auth import hash_password

    with SessionLocal() as db:
        u = db.get(User, 1)
        if u is None:
            u = User(
                id=1,
                email="admin@example.com",
                password_hash=hash_password("adminpass"),
                name="Admin",
            )
            db.add(u)
            db.commit()
        admin_email = u.email

    client = TestClient(app)
    client.post(
        "/login",
        data={"email": admin_email, "password": "adminpass"},
        follow_redirects=False,
    )
    resp = client.post("/admin/test-email", data={"recipient": "not-an-email"})
    assert resp.status_code == 400
    assert recorder.calls == []
