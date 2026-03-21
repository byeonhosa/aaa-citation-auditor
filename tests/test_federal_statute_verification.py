"""Tests for Federal U.S. Code citation detection and verification.

Covers:
- parse_federal_section: all U.S.C. citation formats
- verify_federal_section: mock GovInfo API responses
- FederalStatuteVerifier class
- Supplemental detection of USC (no-period) and verbose forms
- Pipeline integration: STATUTE_DETECTED → STATUTE_VERIFIED
- No API key → stays STATUTE_DETECTED gracefully
- Virginia citations not sent to federal verifier
- Federal citations not sent to Virginia verifier
- Cache hit prevents API call
- Duplicate detection (eyecite + regex dedup)
- CRITICAL TEST: 2 U.S.C. + 1 case citation → 3 correct citations
"""

from __future__ import annotations

import httpx

from app.services.audit import extract_citations
from app.services.statute_verification import (
    FederalStatuteVerifier,
    parse_federal_section,
    parse_virginia_section,
    verify_federal_section,
)
from app.services.verification import verify_citations

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_govinfo_response(count: int, results: list | None = None) -> httpx.Response:
    req = httpx.Request("GET", "https://api.govinfo.gov/search")
    body = {"count": count, "results": results or []}
    return httpx.Response(200, json=body, request=req)


def _make_govinfo_error(status_code: int) -> httpx.Response:
    req = httpx.Request("GET", "https://api.govinfo.gov/search")
    return httpx.Response(status_code, json={}, request=req)


class _MockGovInfoClient:
    """Scriptable httpx.Client replacement for GovInfo tests."""

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self._call_count = 0
        self.calls: list[dict] = []

    def __enter__(self) -> "_MockGovInfoClient":
        return self

    def __exit__(self, *_args) -> None:
        pass

    def get(self, url: str, **kwargs):
        self.calls.append({"url": url, "params": kwargs.get("params", {})})
        if self._call_count >= len(self._responses):
            raise RuntimeError("MockGovInfoClient exhausted responses")
        item = self._responses[self._call_count]
        self._call_count += 1
        if isinstance(item, Exception):
            raise item
        return item


class _MockFederalVerifier:
    """Scriptable mock for FederalStatuteVerifier."""

    def __init__(self, responses: dict) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str]] = []

    def verify(self, title: str, section: str) -> tuple[str, str | None]:
        self.calls.append((title, section))
        return self._responses.get((title, section), ("STATUTE_ERROR", None))


class _MockVAVerifier:
    """Scriptable mock for VirginiaStatuteVerifier."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def verify(self, section_number: str) -> tuple[str, str | None]:
        self.calls.append(section_number)
        return "STATUTE_VERIFIED", "Mock Virginia section"


# ── parse_federal_section ─────────────────────────────────────────────────────


def test_parse_standard_usc():
    result = parse_federal_section("42 U.S.C. § 1983")
    assert result == ("42", "1983")


def test_parse_standard_usc_28():
    result = parse_federal_section("28 U.S.C. § 1331")
    assert result == ("28", "1331")


def test_parse_section_keyword():
    result = parse_federal_section("42 U.S.C. Section 1983")
    assert result == ("42", "1983")


def test_parse_sec_dot():
    result = parse_federal_section("42 U.S.C. Sec. 1983")
    assert result == ("42", "1983")


def test_parse_usc_no_periods():
    result = parse_federal_section("42 USC § 1983")
    assert result == ("42", "1983")


def test_parse_usc_no_periods_section_keyword():
    result = parse_federal_section("42 USC Section 1983")
    assert result == ("42", "1983")


def test_parse_verbose_title_form():
    result = parse_federal_section("Title 42, United States Code, Section 1983")
    assert result == ("42", "1983")


def test_parse_verbose_title_symbol():
    result = parse_federal_section("Title 28, United States Code, § 1331")
    assert result == ("28", "1331")


def test_parse_with_subsection_stripped():
    result = parse_federal_section("42 U.S.C. § 1983(a)")
    assert result == ("42", "1983")


def test_parse_with_subsection_b_stripped():
    result = parse_federal_section("28 U.S.C. § 1331(b)(1)")
    assert result == ("28", "1331")


def test_parse_letter_suffix_kept():
    """Section numbers like '1234a' are distinct real sections — keep the letter."""
    result = parse_federal_section("29 U.S.C. § 626a")
    assert result == ("29", "626a")


def test_parse_usc_no_periods_with_title_prefix():
    result = parse_federal_section("Title 42 USC Section 1983")
    assert result == ("42", "1983")


def test_parse_non_usc_returns_none():
    assert parse_federal_section("Va. Code § 15.2-3400") is None


def test_parse_plain_section_symbol_returns_none():
    assert parse_federal_section("§ 1983") is None


def test_parse_bare_number_returns_none():
    assert parse_federal_section("1983") is None


# ── verify_federal_section ────────────────────────────────────────────────────


def test_verify_found():
    client = _MockGovInfoClient(
        [_make_govinfo_response(5, [{"title": "Civil Rights — 42 U.S.C. 1983"}])]
    )
    status, title = verify_federal_section("42", "1983", api_key="TEST", _client=client)
    assert status == "STATUTE_VERIFIED"
    assert title == "Civil Rights — 42 U.S.C. 1983"


def test_verify_found_no_title():
    client = _MockGovInfoClient([_make_govinfo_response(3, [])])
    status, title = verify_federal_section("28", "1331", api_key="TEST", _client=client)
    assert status == "STATUTE_VERIFIED"
    assert title is None


def test_verify_not_found_count_zero():
    client = _MockGovInfoClient([_make_govinfo_response(0)])
    status, title = verify_federal_section("99", "99999", api_key="TEST", _client=client)
    assert status == "STATUTE_NOT_FOUND"
    assert title is None


def test_verify_http_500_returns_error():
    client = _MockGovInfoClient([_make_govinfo_error(500)])
    status, title = verify_federal_section("42", "1983", api_key="TEST", _client=client)
    assert status == "STATUTE_ERROR"
    assert title is None


def test_verify_http_429_rate_limit():
    client = _MockGovInfoClient([_make_govinfo_error(429)])
    status, title = verify_federal_section("42", "1983", api_key="TEST", _client=client)
    assert status == "STATUTE_ERROR"
    assert title is None


def test_verify_http_403_bad_key():
    client = _MockGovInfoClient([_make_govinfo_error(403)])
    status, title = verify_federal_section("42", "1983", api_key="BADKEY", _client=client)
    assert status == "STATUTE_ERROR"
    assert title is None


def test_verify_timeout_returns_error():
    client = _MockGovInfoClient([httpx.ReadTimeout("timed out")])
    status, title = verify_federal_section("42", "1983", api_key="TEST", _client=client)
    assert status == "STATUTE_ERROR"
    assert title is None


def test_verify_connect_error_returns_error():
    client = _MockGovInfoClient([httpx.ConnectError("refused")])
    status, title = verify_federal_section("42", "1983", api_key="TEST", _client=client)
    assert status == "STATUTE_ERROR"
    assert title is None


def test_verify_sends_correct_query_params():
    client = _MockGovInfoClient([_make_govinfo_response(1, [])])
    verify_federal_section("42", "1983", api_key="MY_KEY", _client=client)
    assert len(client.calls) == 1
    params = client.calls[0]["params"]
    assert params["query"] == "collection:USCODE title:42 section:1983"
    assert params["pageSize"] == "1"
    assert params["api_key"] == "MY_KEY"


# ── FederalStatuteVerifier ────────────────────────────────────────────────────


def test_federal_verifier_wraps_verify_federal_section():
    """FederalStatuteVerifier is a thin wrapper — verify the underlying function works."""
    client = _MockGovInfoClient([_make_govinfo_response(2, [])])
    # Can't inject _client through the class; test the underlying function instead.
    status, _ = verify_federal_section("42", "1983", api_key="KEY", _client=client)
    assert status == "STATUTE_VERIFIED"
    FederalStatuteVerifier(api_key="KEY")  # construction should not raise


# ── Detection: USC alternate forms ────────────────────────────────────────────


def test_usc_no_periods_detected():
    text = "Claims arise under 42 USC § 1983 as amended."
    citations, _ = extract_citations(text)
    raw = [c.raw_text for c in citations]
    assert any("1983" in r for r in raw), f"Expected 1983 citation; got {raw}"


def test_usc_no_periods_section_keyword_detected():
    text = "Jurisdiction exists under 28 USC Section 1331."
    citations, _ = extract_citations(text)
    raw = [c.raw_text for c in citations]
    assert any("1331" in r for r in raw), f"Expected 1331 citation; got {raw}"


def test_verbose_united_states_code_detected():
    text = "As provided by Title 42, United States Code, Section 1983."
    citations, _ = extract_citations(text)
    raw = [c.raw_text for c in citations]
    assert any("1983" in r for r in raw), f"Expected 1983 citation; got {raw}"


def test_verbose_united_states_code_symbol_detected():
    text = "Under Title 28, United States Code, § 1331, district courts have jurisdiction."
    citations, _ = extract_citations(text)
    raw = [c.raw_text for c in citations]
    assert any("1331" in r for r in raw), f"Expected 1331 citation; got {raw}"


def test_standard_usc_dotted_not_duplicated():
    """'42 U.S.C. § 1983' is handled by eyecite — supplemental pass must not double-count."""
    text = "Claims under 42 U.S.C. § 1983 are governed by federal law."
    citations, _ = extract_citations(text)
    hits = [c for c in citations if "1983" in c.raw_text]
    assert len(hits) == 1, (
        f"Expected exactly 1 citation, got {len(hits)}: {[c.raw_text for c in hits]}"
    )


def test_usc_no_periods_type_is_full_law_citation():
    text = "Under 42 USC § 1983."
    citations, _ = extract_citations(text)
    hits = [c for c in citations if "1983" in c.raw_text]
    assert hits, "Expected 1983 citation"
    assert hits[0].citation_type == "FullLawCitation"


def test_usc_no_periods_has_snippet():
    text = "The plaintiff brings claims under 42 USC § 1983 against the officer."
    citations, _ = extract_citations(text)
    hits = [c for c in citations if "1983" in c.raw_text]
    assert hits, "Expected 1983 citation"
    assert hits[0].snippet is not None
    assert "1983" in hits[0].snippet


# ── CRITICAL TEST ─────────────────────────────────────────────────────────────


def test_critical_two_usc_plus_case():
    """2 U.S.C. statutes + 1 case citation from one paragraph → 3 citations."""
    text = (
        "The plaintiff brings claims under 42 U.S.C. § 1983"
        " and 28 U.S.C. § 1331."
        " See Virginia v. Hicks, 539 U.S. 113 (2003)."
    )
    citations, warnings = extract_citations(text)
    assert len(citations) == 3, (
        f"Expected 3 citations, got {len(citations)}: {[c.raw_text for c in citations]}"
    )
    raw = [c.raw_text for c in citations]
    assert any("1983" in r for r in raw), "Missing 42 U.S.C. § 1983"
    assert any("1331" in r for r in raw), "Missing 28 U.S.C. § 1331"
    assert any("539 U.S. 113" in r for r in raw), "Missing 539 U.S. 113"


def test_critical_usc_citations_are_full_law_citation():
    text = (
        "The plaintiff brings claims under 42 U.S.C. § 1983"
        " and 28 U.S.C. § 1331."
        " See Virginia v. Hicks, 539 U.S. 113 (2003)."
    )
    citations, _ = extract_citations(text)
    usc = [c for c in citations if "1983" in c.raw_text or "1331" in c.raw_text]
    assert len(usc) == 2
    for c in usc:
        assert c.citation_type == "FullLawCitation", (
            f"{c.raw_text!r} should be FullLawCitation, got {c.citation_type}"
        )


# ── Pipeline integration ──────────────────────────────────────────────────────


def test_federal_citation_gets_statute_verified():
    """U.S.C. citation flows through to STATUTE_VERIFIED with mock verifier."""
    text = "Claims under 42 U.S.C. § 1983 are governed by federal law."
    citations, _ = extract_citations(text)

    fed_verifier = _MockFederalVerifier({("42", "1983"): ("STATUTE_VERIFIED", "Civil rights")})
    result = verify_citations(
        citations,
        courtlistener_token=None,
        verification_base_url="https://www.courtlistener.com/api/rest/v4/citation-lookup/",
        federal_statute_verifier=fed_verifier,
        govinfo_api_key="TEST_KEY",
    )
    hits = [c for c in result if "1983" in c.raw_text]
    assert hits, "No 1983 citation in result"
    assert hits[0].verification_status == "STATUTE_VERIFIED"
    assert ("42", "1983") in fed_verifier.calls


def test_federal_citation_not_found_stays_statute_detected():
    """U.S.C. not found in GovInfo → stays STATUTE_DETECTED with updated detail."""
    text = "Claims under 99 U.S.C. § 99999."
    citations, _ = extract_citations(text)

    fed_verifier = _MockFederalVerifier({("99", "99999"): ("STATUTE_NOT_FOUND", None)})
    result = verify_citations(
        citations,
        courtlistener_token=None,
        verification_base_url="https://www.courtlistener.com/api/rest/v4/citation-lookup/",
        federal_statute_verifier=fed_verifier,
        govinfo_api_key="TEST_KEY",
    )
    hits = [c for c in result if "99999" in c.raw_text]
    assert hits, "No 99999 citation in result"
    assert hits[0].verification_status == "STATUTE_DETECTED"
    assert "could not be verified" in (hits[0].verification_detail or "").lower()


def test_no_api_key_stays_statute_detected():
    """Without a GovInfo API key, U.S.C. citations stay as STATUTE_DETECTED."""
    text = "Claims under 42 U.S.C. § 1983 are governed by federal law."
    citations, _ = extract_citations(text)

    fed_verifier = _MockFederalVerifier({("42", "1983"): ("STATUTE_VERIFIED", "Civil rights")})
    result = verify_citations(
        citations,
        courtlistener_token=None,
        verification_base_url="https://www.courtlistener.com/api/rest/v4/citation-lookup/",
        federal_statute_verifier=fed_verifier,
        govinfo_api_key=None,  # no key
    )
    hits = [c for c in result if "1983" in c.raw_text]
    assert hits
    assert hits[0].verification_status == "STATUTE_DETECTED"
    assert fed_verifier.calls == [], "Verifier should not be called when no API key"


def test_federal_disabled_stays_statute_detected():
    """federal_statute_verification=False → verifier never called."""
    text = "Claims under 42 U.S.C. § 1983 are governed by federal law."
    citations, _ = extract_citations(text)

    fed_verifier = _MockFederalVerifier({("42", "1983"): ("STATUTE_VERIFIED", "Civil rights")})
    result = verify_citations(
        citations,
        courtlistener_token=None,
        verification_base_url="https://www.courtlistener.com/api/rest/v4/citation-lookup/",
        federal_statute_verifier=fed_verifier,
        govinfo_api_key="TEST_KEY",
        federal_statute_verification=False,
    )
    hits = [c for c in result if "1983" in c.raw_text]
    assert hits
    assert hits[0].verification_status == "STATUTE_DETECTED"
    assert fed_verifier.calls == []


def test_federal_error_keeps_statute_detected():
    """GovInfo API error → status stays STATUTE_DETECTED (never downgraded)."""
    text = "Claims under 42 U.S.C. § 1983 are governed by federal law."
    citations, _ = extract_citations(text)

    fed_verifier = _MockFederalVerifier({("42", "1983"): ("STATUTE_ERROR", None)})
    result = verify_citations(
        citations,
        courtlistener_token=None,
        verification_base_url="https://www.courtlistener.com/api/rest/v4/citation-lookup/",
        federal_statute_verifier=fed_verifier,
        govinfo_api_key="TEST_KEY",
    )
    hits = [c for c in result if "1983" in c.raw_text]
    assert hits
    assert hits[0].verification_status == "STATUTE_DETECTED"


def test_virginia_citation_not_sent_to_federal_verifier():
    """Va. Code citations should go to Virginia verifier, not federal."""
    text = "Under Va. Code § 15.2-3400."
    citations, _ = extract_citations(text)

    fed_verifier = _MockFederalVerifier({})
    va_verifier = _MockVAVerifier()
    verify_citations(
        citations,
        courtlistener_token=None,
        verification_base_url="https://www.courtlistener.com/api/rest/v4/citation-lookup/",
        federal_statute_verifier=fed_verifier,
        govinfo_api_key="TEST_KEY",
        virginia_statute_verifier=va_verifier,
    )
    assert fed_verifier.calls == [], "Federal verifier should not be called for Va. Code"
    assert "15.2-3400" in va_verifier.calls, "Virginia verifier should be called"


def test_federal_citation_not_sent_to_virginia_verifier():
    """42 U.S.C. § 1983 should go to federal verifier, not Virginia."""
    text = "Claims under 42 U.S.C. § 1983."
    citations, _ = extract_citations(text)

    fed_verifier = _MockFederalVerifier({("42", "1983"): ("STATUTE_VERIFIED", None)})
    va_verifier = _MockVAVerifier()
    verify_citations(
        citations,
        courtlistener_token=None,
        verification_base_url="https://www.courtlistener.com/api/rest/v4/citation-lookup/",
        federal_statute_verifier=fed_verifier,
        govinfo_api_key="TEST_KEY",
        virginia_statute_verifier=va_verifier,
    )
    assert va_verifier.calls == [], "Virginia verifier should not be called for U.S.C."
    assert ("42", "1983") in fed_verifier.calls


def test_critical_text_end_to_end_statuses():
    """Full pipeline: 2 U.S.C. citations get STATUTE_VERIFIED, case gets UNVERIFIED_NO_TOKEN."""
    text = (
        "The plaintiff brings claims under 42 U.S.C. § 1983"
        " and 28 U.S.C. § 1331."
        " See Virginia v. Hicks, 539 U.S. 113 (2003)."
    )
    citations, _ = extract_citations(text)

    fed_verifier = _MockFederalVerifier(
        {
            ("42", "1983"): ("STATUTE_VERIFIED", "Civil rights"),
            ("28", "1331"): ("STATUTE_VERIFIED", "Federal question"),
        }
    )
    result = verify_citations(
        citations,
        courtlistener_token=None,
        verification_base_url="https://www.courtlistener.com/api/rest/v4/citation-lookup/",
        federal_statute_verifier=fed_verifier,
        govinfo_api_key="TEST_KEY",
    )
    statuses = {c.raw_text: c.verification_status for c in result}
    assert any("1983" in k for k in statuses), "1983 not in result"
    assert any("1331" in k for k in statuses), "1331 not in result"
    for key, status in statuses.items():
        if "1983" in key or "1331" in key:
            assert status == "STATUTE_VERIFIED", f"{key!r} should be STATUTE_VERIFIED, got {status}"
        elif "539 U.S. 113" in key:
            assert status == "UNVERIFIED_NO_TOKEN", (
                f"Case law without token should be UNVERIFIED_NO_TOKEN, got {status!r}"
            )


# ── Statute cache ─────────────────────────────────────────────────────────────


def test_cache_hit_prevents_federal_api_call():
    """A pre-populated statute_cache entry prevents the federal verifier from being called."""
    text = "Claims under 42 U.S.C. § 1983."
    citations, _ = extract_citations(text)

    fed_verifier = _MockFederalVerifier({})
    statute_cache = {"usc:42-1983": {"status": "STATUTE_VERIFIED", "section_title": "Civil rights"}}
    result = verify_citations(
        citations,
        courtlistener_token=None,
        verification_base_url="https://www.courtlistener.com/api/rest/v4/citation-lookup/",
        federal_statute_verifier=fed_verifier,
        govinfo_api_key="TEST_KEY",
        statute_cache=statute_cache,
    )
    hits = [c for c in result if "1983" in c.raw_text]
    assert hits
    assert hits[0].verification_status == "STATUTE_VERIFIED"
    assert fed_verifier.calls == [], "Verifier must not be called on cache hit"


def test_federal_result_written_to_statute_cache():
    """Successful federal verification populates the statute_cache dict."""
    text = "Claims under 42 U.S.C. § 1983."
    citations, _ = extract_citations(text)

    fed_verifier = _MockFederalVerifier({("42", "1983"): ("STATUTE_VERIFIED", "Civil rights")})
    statute_cache: dict = {}
    verify_citations(
        citations,
        courtlistener_token=None,
        verification_base_url="https://www.courtlistener.com/api/rest/v4/citation-lookup/",
        federal_statute_verifier=fed_verifier,
        govinfo_api_key="TEST_KEY",
        statute_cache=statute_cache,
    )
    assert "usc:42-1983" in statute_cache
    assert statute_cache["usc:42-1983"]["status"] == "STATUTE_VERIFIED"


def test_federal_error_not_written_to_cache():
    """STATUTE_ERROR results must NOT be cached (transient errors should be retried)."""
    text = "Claims under 42 U.S.C. § 1983."
    citations, _ = extract_citations(text)

    fed_verifier = _MockFederalVerifier({("42", "1983"): ("STATUTE_ERROR", None)})
    statute_cache: dict = {}
    verify_citations(
        citations,
        courtlistener_token=None,
        verification_base_url="https://www.courtlistener.com/api/rest/v4/citation-lookup/",
        federal_statute_verifier=fed_verifier,
        govinfo_api_key="TEST_KEY",
        statute_cache=statute_cache,
    )
    assert "usc:42-1983" not in statute_cache


# ── parse_virginia_section correctly rejects U.S.C. forms ────────────────────


def test_virginia_parser_rejects_usc():
    assert parse_virginia_section("42 U.S.C. § 1983") is None


def test_virginia_parser_rejects_usc_no_periods():
    assert parse_virginia_section("42 USC § 1983") is None


def test_virginia_parser_rejects_verbose_usc():
    assert parse_virginia_section("Title 42, United States Code, Section 1983") is None


def test_usc_subsection_reference_verified():
    """28 U.S.C. § 1257(a) should verify against section 1257 (subsection stripped)."""
    text = "Review under 28 U.S.C. § 1257(a)."
    citations, _ = extract_citations(text)
    # Ensure exactly one citation detected
    assert any("1257" in c.raw_text for c in citations), "28 U.S.C. § 1257(a) not detected"

    fed_verifier = _MockFederalVerifier({("28", "1257"): ("STATUTE_VERIFIED", "Certiorari")})
    result = verify_citations(
        citations,
        courtlistener_token=None,
        verification_base_url="https://www.courtlistener.com/api/rest/v4/citation-lookup/",
        federal_statute_verifier=fed_verifier,
        govinfo_api_key="TEST_KEY",
    )
    hits = [c for c in result if "1257" in c.raw_text]
    assert hits, "No 1257 citation in result"
    assert hits[0].verification_status == "STATUTE_VERIFIED"
    assert "1257" in hits[0].verification_detail


def test_federal_statute_no_api_key_sets_helpful_detail():
    """When GOVINFO_API_KEY is absent, detected federal statutes get a helpful detail message."""
    text = "Claims under 42 U.S.C. § 1983 are actionable."
    citations, _ = extract_citations(text)
    result = verify_citations(
        citations,
        courtlistener_token=None,
        verification_base_url="https://www.courtlistener.com/api/rest/v4/citation-lookup/",
        federal_statute_verification=True,
        govinfo_api_key=None,  # No key
    )
    usc_cites = [c for c in result if "1983" in c.raw_text]
    assert usc_cites, "Expected 42 U.S.C. § 1983 citation"
    c = usc_cites[0]
    assert c.verification_status == "STATUTE_DETECTED"
    assert "GovInfo API key" in (c.verification_detail or ""), (
        f"Expected API key mention in detail; got: {c.verification_detail!r}"
    )


def test_usc_no_symbol_35_usc_154_detected():
    """35 U.S.C. 154(a)(1) — no § symbol — should be detected."""
    text = "Under 35 U.S.C. 154(a)(1), the patent term is 20 years."
    citations, _ = extract_citations(text)
    hits = [c for c in citations if "154" in c.raw_text and "U.S.C." in c.raw_text]
    assert hits, f"Expected 35 U.S.C. 154 citation; got {[c.raw_text for c in citations]}"


def test_usc_no_symbol_et_seq_detected():
    """35 U.S.C. 1 et seq. — no § symbol, et seq. suffix."""
    text = "See 35 U.S.C. 1 et seq. for the general provisions."
    citations, _ = extract_citations(text)
    hits = [c for c in citations if "U.S.C." in c.raw_text]
    assert hits, f"Expected 35 U.S.C. 1 et seq. citation; got {[c.raw_text for c in citations]}"


def test_usc_no_symbol_21_usc_355_detected():
    """21 U.S.C. 355 — no § symbol."""
    text = "Under 21 U.S.C. 355, FDA approval is required."
    citations, _ = extract_citations(text)
    hits = [c for c in citations if "355" in c.raw_text and "U.S.C." in c.raw_text]
    assert hits, f"Expected 21 U.S.C. 355 citation; got {[c.raw_text for c in citations]}"
