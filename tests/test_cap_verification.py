"""Tests for app.services.cap_verification — CAPVerifier and helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.services.cap_verification import (
    CAPVerifier,
    _get_json,
    _parse_results,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_response(
    *,
    status_code: int = 200,
    json_data=None,
    content_type: str = "application/json",
    is_redirect: bool = False,
):
    """Build a minimal mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.is_redirect = is_redirect
    resp.is_success = 200 <= status_code < 300 and not is_redirect
    resp.headers = {"content-type": content_type}
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("no body")
    return resp


_SINGLE_RESULT = {
    "count": 1,
    "results": [
        {
            "id": 999,
            "name_abbreviation": "Brown v. Board of Education",
            "court": {"name_abbreviation": "SCOTUS"},
            "decision_date": "1954-05-17",
            "citations": [{"cite": "347 U.S. 483"}],
        }
    ],
}

_TWO_RESULTS = {
    "count": 2,
    "results": [
        {
            "id": 1001,
            "name_abbreviation": "Roe v. Wade",
            "court": {"name_abbreviation": "SCOTUS"},
            "decision_date": "1973-01-22",
            "citations": [{"cite": "410 U.S. 113"}],
        },
        {
            "id": 1002,
            "name_abbreviation": "Roe v. Wade",
            "court": {"name_abbreviation": "SCOTUS"},
            "decision_date": "1973-01-22",
            "citations": [{"cite": "410 U.S. 113"}],
        },
    ],
}


# ── _get_json ─────────────────────────────────────────────────────────────────


def test_get_json_returns_dict_on_success():
    import app.services.cap_verification as mod

    mod._cap_unavailable_warned = False
    with patch("httpx.get", return_value=_make_response(json_data={"count": 0, "results": []})):
        result = _get_json("https://api.example.com/", {}, 10)
    assert result == {"count": 0, "results": []}


def test_get_json_returns_none_on_redirect():
    import app.services.cap_verification as mod

    mod._cap_unavailable_warned = False
    with patch("httpx.get", return_value=_make_response(status_code=301, is_redirect=True)):
        result = _get_json("https://api.example.com/", {}, 10)
    assert result is None


def test_get_json_returns_none_on_html_content_type():
    import app.services.cap_verification as mod

    mod._cap_unavailable_warned = False
    resp = _make_response(status_code=200, content_type="text/html")
    resp.is_success = True
    with patch("httpx.get", return_value=resp):
        result = _get_json("https://api.example.com/", {}, 10)
    assert result is None


def test_get_json_returns_none_on_timeout():
    import httpx

    import app.services.cap_verification as mod

    mod._cap_unavailable_warned = False
    with patch("httpx.get", side_effect=httpx.TimeoutException("timed out")):
        result = _get_json("https://api.example.com/", {}, 10)
    assert result is None


def test_get_json_returns_none_on_connection_error():
    import httpx

    import app.services.cap_verification as mod

    mod._cap_unavailable_warned = False
    with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
        result = _get_json("https://api.example.com/", {}, 10)
    assert result is None


# ── _parse_results ────────────────────────────────────────────────────────────


def test_parse_results_single():
    candidates = _parse_results(_SINGLE_RESULT)
    assert len(candidates) == 1
    c = candidates[0]
    assert c["cluster_id"] == 999
    assert c["case_name"] == "Brown v. Board of Education"
    assert c["court"] == "SCOTUS"
    assert c["date_filed"] == "1954-05-17"
    assert c["citation"] == "347 U.S. 483"


def test_parse_results_empty():
    assert _parse_results({"count": 0, "results": []}) == []


def test_parse_results_skips_missing_id():
    data = {"count": 1, "results": [{"name_abbreviation": "No ID Case"}]}
    assert _parse_results(data) == []


# ── CAPVerifier.lookup_by_cite ────────────────────────────────────────────────


def test_lookup_by_cite_verified():
    import app.services.cap_verification as mod

    mod._cap_unavailable_warned = False
    verifier = CAPVerifier()
    with patch("httpx.get", return_value=_make_response(json_data=_SINGLE_RESULT)):
        result = verifier.lookup_by_cite("347 U.S. 483")
    assert result is not None
    assert result.status == "VERIFIED"
    assert result.candidate_cluster_ids == [999]
    assert "Brown v. Board" in result.detail


def test_lookup_by_cite_ambiguous():
    import app.services.cap_verification as mod

    mod._cap_unavailable_warned = False
    verifier = CAPVerifier()
    with patch("httpx.get", return_value=_make_response(json_data=_TWO_RESULTS)):
        result = verifier.lookup_by_cite("410 U.S. 113")
    assert result is not None
    assert result.status == "AMBIGUOUS"
    assert len(result.candidate_cluster_ids) == 2


def test_lookup_by_cite_not_found():
    import app.services.cap_verification as mod

    mod._cap_unavailable_warned = False
    verifier = CAPVerifier()
    with patch("httpx.get", return_value=_make_response(json_data={"count": 0, "results": []})):
        result = verifier.lookup_by_cite("999 U.S. 999")
    assert result is None


def test_lookup_by_cite_unavailable_returns_none():
    import httpx

    import app.services.cap_verification as mod

    mod._cap_unavailable_warned = False
    verifier = CAPVerifier()
    with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
        result = verifier.lookup_by_cite("347 U.S. 483")
    assert result is None


# ── CAPVerifier.verify_citation (two-step) ───────────────────────────────────


def test_verify_citation_uses_name_when_cite_fails():
    import app.services.cap_verification as mod

    mod._cap_unavailable_warned = False
    verifier = CAPVerifier()
    no_results = {"count": 0, "results": []}
    with patch("httpx.get") as mock_get:
        mock_get.side_effect = [
            _make_response(json_data=no_results),  # cite lookup → nothing
            _make_response(json_data=_SINGLE_RESULT),  # name lookup → hit
        ]
        result = verifier.verify_citation("999 U.S. 999", "Brown v. Board of Education")
    assert result is not None
    assert result.status == "VERIFIED"
    assert mock_get.call_count == 2


def test_verify_citation_returns_none_when_both_fail():
    import app.services.cap_verification as mod

    mod._cap_unavailable_warned = False
    verifier = CAPVerifier()
    no_results = {"count": 0, "results": []}
    with patch("httpx.get", return_value=_make_response(json_data=no_results)):
        result = verifier.verify_citation("999 U.S. 999", "Fake Case v. Nobody")
    assert result is None


# ── API key propagation ───────────────────────────────────────────────────────


def test_api_key_included_in_request():
    import app.services.cap_verification as mod

    mod._cap_unavailable_warned = False
    verifier = CAPVerifier(api_key="test-key-123")
    with patch("httpx.get", return_value=_make_response(json_data=_SINGLE_RESULT)) as mock_get:
        verifier.lookup_by_cite("347 U.S. 483")
    call_kwargs = mock_get.call_args
    params = call_kwargs[1].get("params") or call_kwargs[0][1]
    assert params.get("api_key") == "test-key-123"


# ── Provenance integration ────────────────────────────────────────────────────


def test_cap_fallback_provenance_label():
    from app.services.provenance import get_provenance

    info = get_provenance("VERIFIED", "cap_fallback")
    assert info.label == "CAP Match"
    assert info.css_class == "provenance-cap"
