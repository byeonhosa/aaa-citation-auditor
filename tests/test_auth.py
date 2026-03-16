"""Tests for user authentication: register, login, logout, and access control."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from aaa_db.models import AuditRun, User
from aaa_db.session import SessionLocal
from app.main import app
from app.services.auth import (
    authenticate_user,
    create_user,
    get_user_by_email,
    hash_password,
    users_exist,
    verify_password,
)

# ---------------------------------------------------------------------------
# Unit tests for auth service helpers
# ---------------------------------------------------------------------------


def _mem_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from aaa_db.models import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_hash_and_verify_password():
    hashed = hash_password("correcthorsebatterystaple")
    assert verify_password("correcthorsebatterystaple", hashed)
    assert not verify_password("wrongpassword", hashed)


def test_create_user_stores_hashed_password():
    db = _mem_session()
    user = create_user(db, email="alice@example.com", password="secret123", name="Alice")
    assert user.id is not None
    assert user.email == "alice@example.com"
    assert user.password_hash != "secret123"
    assert user.is_active is True


def test_create_user_lowercases_email():
    db = _mem_session()
    user = create_user(db, email="Alice@Example.COM", password="secret123", name="Alice")
    assert user.email == "alice@example.com"


def test_get_user_by_email():
    db = _mem_session()
    create_user(db, email="bob@example.com", password="pass1234", name="Bob")
    found = get_user_by_email(db, "bob@example.com")
    assert found is not None
    assert found.name == "Bob"
    assert get_user_by_email(db, "nobody@example.com") is None


def test_authenticate_user_success():
    db = _mem_session()
    create_user(db, email="carol@example.com", password="mypassword", name="Carol")
    user = authenticate_user(db, "carol@example.com", "mypassword")
    assert user is not None
    assert user.email == "carol@example.com"


def test_authenticate_user_wrong_password():
    db = _mem_session()
    create_user(db, email="dave@example.com", password="rightpass", name="Dave")
    assert authenticate_user(db, "dave@example.com", "wrongpass") is None


def test_authenticate_user_unknown_email():
    db = _mem_session()
    assert authenticate_user(db, "ghost@example.com", "anypass") is None


def test_authenticate_user_inactive():
    db = _mem_session()
    user = create_user(db, email="eve@example.com", password="pass1234", name="Eve")
    user.is_active = False
    db.commit()
    assert authenticate_user(db, "eve@example.com", "pass1234") is None


def test_users_exist():
    db = _mem_session()
    assert not users_exist(db)
    create_user(db, email="frank@example.com", password="pass1234", name="Frank")
    assert users_exist(db)


# ---------------------------------------------------------------------------
# Integration tests via FastAPI TestClient
# ---------------------------------------------------------------------------

# Unique email prefix to avoid collisions with other tests
_EMAIL_DOMAIN = "@authtest.invalid"


@pytest.fixture(autouse=True)
def _clean_users():
    """Delete all auth-test users and their runs before/after each test."""
    with SessionLocal() as db:
        # Remove runs whose user_id points to a test user first
        test_users = db.scalars(
            select(User).where(User.email.like(f"%{_EMAIL_DOMAIN}"))
        ).all()
        test_ids = [u.id for u in test_users]
        if test_ids:
            db.execute(delete(AuditRun).where(AuditRun.user_id.in_(test_ids)))
        db.execute(delete(User).where(User.email.like(f"%{_EMAIL_DOMAIN}")))
        db.commit()
    yield
    with SessionLocal() as db:
        test_users = db.scalars(
            select(User).where(User.email.like(f"%{_EMAIL_DOMAIN}"))
        ).all()
        test_ids = [u.id for u in test_users]
        if test_ids:
            db.execute(delete(AuditRun).where(AuditRun.user_id.in_(test_ids)))
        db.execute(delete(User).where(User.email.like(f"%{_EMAIL_DOMAIN}")))
        db.commit()


def _tc():
    return TestClient(app, raise_server_exceptions=True)


def _email(name: str) -> str:
    return f"{name}{_EMAIL_DOMAIN}"


def _register(client, name="tester", password="password123"):
    return client.post(
        "/register",
        data={"email": _email(name), "password": password, "name": name.title()},
        follow_redirects=False,
    )


def _login(client, name="tester", password="password123"):
    return client.post(
        "/login",
        data={"email": _email(name), "password": password},
        follow_redirects=False,
    )


def test_register_creates_user_and_redirects():
    with _tc() as c:
        resp = _register(c)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    with SessionLocal() as db:
        user = get_user_by_email(db, _email("tester"))
    assert user is not None


def test_register_duplicate_email_shows_error():
    with _tc() as c:
        _register(c)
        resp = _register(c)
    assert resp.status_code == 200
    assert b"already exists" in resp.content


def test_register_short_password_shows_error():
    with _tc() as c:
        resp = c.post(
            "/register",
            data={"email": _email("short"), "password": "abc", "name": ""},
            follow_redirects=False,
        )
    assert resp.status_code == 200
    assert b"8 characters" in resp.content


def test_login_success_redirects():
    with _tc() as c:
        _register(c)
        resp = _login(c)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_login_wrong_password_shows_error():
    with _tc() as c:
        _register(c)
        resp = _login(c, password="wrongpassword")
    assert resp.status_code == 200
    assert b"Invalid email or password" in resp.content


def test_logout_clears_session_and_redirects():
    with _tc() as c:
        _register(c)
        _login(c)
        resp = c.get("/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


def test_unauthenticated_redirect_when_users_exist():
    with _tc() as c:
        _register(c)
        # Use a fresh client (no session cookie) to simulate logged-out state
        with _tc() as c2:
            resp = c2.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


def test_history_filters_by_user():
    with SessionLocal() as db:
        user_b = create_user(db, email=_email("userb"), password="pass1234", name="UserB")
        run_b = AuditRun(user_id=user_b.id, source_type="text", citation_count=0)
        db.add(run_b)
        db.commit()
        run_b_id = run_b.id

    with _tc() as c:
        _register(c, name="usera")
        _login(c, name="usera")
        resp = c.get("/history", follow_redirects=False)

    assert resp.status_code == 200
    assert f"Run #{run_b_id}".encode() not in resp.content


def test_cross_user_run_access_denied():
    with SessionLocal() as db:
        user_b = create_user(db, email=_email("userb2"), password="pass1234", name="UserB2")
        run = AuditRun(user_id=user_b.id, source_type="text", citation_count=0)
        db.add(run)
        db.commit()
        run_id = run.id

    with _tc() as c:
        _register(c, name="usera2")
        _login(c, name="usera2")
        resp = c.get(f"/history/{run_id}", follow_redirects=False)

    assert resp.status_code == 404


def test_audit_associates_run_with_user():
    with _tc() as c:
        _register(c)
        _login(c)
        resp = c.post(
            "/audit",
            data={"pasted_text": "Smith v. Jones, 123 F.3d 456 (9th Cir. 1997)."},
            follow_redirects=False,
        )
    # Should either redirect to result or return 200 dashboard
    assert resp.status_code in (200, 303)
    with SessionLocal() as db:
        user = get_user_by_email(db, _email("tester"))
        assert user is not None
        run = db.scalar(
            select(AuditRun)
            .where(AuditRun.user_id == user.id)
            .order_by(AuditRun.id.desc())
        )
    assert run is not None
