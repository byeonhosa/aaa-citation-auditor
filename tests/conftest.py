"""Shared test configuration.

Patches the database session to use an in-memory SQLite engine (with
StaticPool so all connections share one database) before any production
modules are imported.  Tests never touch the production aaa.db file.
"""

from __future__ import annotations

import os

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# ── Must be set before aaa_db or app modules are imported ─────────────────
# Override .env values so tests never touch production DB or real APIs.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["COURTLISTENER_TOKEN"] = ""  # disable real CourtListener calls
os.environ["AI_PROVIDER"] = "none"  # disable real AI calls
os.environ["OPENAI_API_KEY"] = ""

# ── In-memory test engine (shared across all connections via StaticPool) ───
_TEST_ENGINE = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_TEST_ENGINE)

# Patch aaa_db.session so every subsequent import gets the test engine.
import aaa_db.session as _aaa_session  # noqa: E402

_aaa_session.engine = _TEST_ENGINE
_aaa_session.SessionLocal = _TestSessionLocal


# ── Per-test database isolation ────────────────────────────────────────────
@pytest.fixture(autouse=True)
def clean_db() -> None:
    from aaa_db.models import (
        AppSettings,
        AuditRun,
        Base,
        CitationResolutionCache,
        CitationResultRecord,
        StatuteVerificationCache,
        TelemetryEvent,
    )

    Base.metadata.create_all(bind=_TEST_ENGINE)
    with _TestSessionLocal() as db:
        db.query(CitationResultRecord).delete()
        db.query(AuditRun).delete()
        db.query(TelemetryEvent).delete()
        db.query(CitationResolutionCache).delete()
        db.query(StatuteVerificationCache).delete()
        db.query(AppSettings).delete()
        db.commit()


# ── HTTP mock helpers ──────────────────────────────────────────────────────
class _MockClient:
    """Scriptable replacement for httpx.Client that returns scripted responses."""

    def __init__(self, responses: list, **_kwargs) -> None:
        self._responses = list(responses)
        self._call_count = 0

    def __enter__(self) -> _MockClient:
        return self

    def __exit__(self, *_args) -> None:
        pass

    def post(self, url: str, **_kwargs):
        if self._call_count >= len(self._responses):
            raise RuntimeError("MockClient exhausted responses")
        item = self._responses[self._call_count]
        self._call_count += 1
        if isinstance(item, Exception):
            raise item
        return item


def _make_cl_response(status_code: int, json_data=None) -> httpx.Response:
    req = httpx.Request("POST", "https://example.test/")
    return httpx.Response(status_code, json=json_data, request=req)


# ── CourtListener mock fixtures ────────────────────────────────────────────


@pytest.fixture()
def mock_courtlistener_verified(monkeypatch) -> None:
    """CourtListener returns a single verified match."""
    responses = [_make_cl_response(200, [{"status": 200, "clusters": [{"id": 1}]}])]
    monkeypatch.setattr(
        "app.services.http_client.httpx.Client",
        lambda **kw: _MockClient(responses, **kw),
    )
    monkeypatch.setattr("app.services.http_client.time.sleep", lambda _: None)


@pytest.fixture()
def mock_courtlistener_multiple_matches(monkeypatch) -> None:
    """CourtListener returns multiple matches (ambiguous)."""
    responses = [
        _make_cl_response(
            200,
            [
                {
                    "status": 300,
                    "clusters": [{"id": 1}, {"id": 2}],
                    "error_message": "Multiple possible matches.",
                }
            ],
        )
    ]
    monkeypatch.setattr(
        "app.services.http_client.httpx.Client",
        lambda **kw: _MockClient(responses, **kw),
    )
    monkeypatch.setattr("app.services.http_client.time.sleep", lambda _: None)


@pytest.fixture()
def mock_courtlistener_not_found(monkeypatch) -> None:
    """CourtListener returns not found."""
    responses = [_make_cl_response(200, [{"status": 404, "error_message": "No match found."}])]
    monkeypatch.setattr(
        "app.services.http_client.httpx.Client",
        lambda **kw: _MockClient(responses, **kw),
    )
    monkeypatch.setattr("app.services.http_client.time.sleep", lambda _: None)


@pytest.fixture()
def mock_courtlistener_unreachable(monkeypatch) -> None:
    """CourtListener is unreachable (all retries time out)."""
    responses = [
        httpx.ReadTimeout("timed out"),
        httpx.ReadTimeout("timed out"),
        httpx.ReadTimeout("timed out"),
    ]
    monkeypatch.setattr(
        "app.services.http_client.httpx.Client",
        lambda **kw: _MockClient(responses, **kw),
    )
    monkeypatch.setattr("app.services.http_client.time.sleep", lambda _: None)
