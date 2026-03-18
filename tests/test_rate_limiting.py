"""Tests for CourtListener rate limiting behavior."""

from __future__ import annotations

import unittest.mock as mock

from app.services.verification import (
    _RATE_LIMITED_DETAIL,
    CourtListenerVerifier,
    map_courtlistener_result,
)


def _make_mock_response(status_code: int) -> mock.Mock:
    resp = mock.Mock()
    resp.status_code = status_code
    return resp


class TestRateLimitedStatus:
    def test_map_courtlistener_result_429_returns_rate_limited(self):
        result = map_courtlistener_result({"status": 429})
        assert result.status == "RATE_LIMITED"
        assert "rate-limited" in result.detail.lower() or "rate limited" in result.detail.lower()

    def test_handle_single_response_429(self):
        verifier = CourtListenerVerifier(token="test", base_url="http://example.com")
        resp = _make_mock_response(429)
        result = verifier._handle_single_response(resp)
        assert result.status == "RATE_LIMITED"
        assert "rate" in result.detail.lower()

    def test_handle_batch_response_429(self):
        verifier = CourtListenerVerifier(token="test", base_url="http://example.com")
        resp = _make_mock_response(429)
        results = verifier._handle_batch_response(resp, 5)
        assert len(results) == 5
        assert all(r.status == "RATE_LIMITED" for r in results)

    def test_rate_limited_detail_message(self):
        assert "resolution cache" in _RATE_LIMITED_DETAIL.lower()
