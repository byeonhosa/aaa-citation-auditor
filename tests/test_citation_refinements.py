"""Tests for citation-refinements branch: Issues A, B, and C."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.audit import CitationResult
from app.services.verification import VerificationResponse

client = TestClient(app)


# ── Issue A: Search fallback precision ───────────────────────────────────────


def test_extract_case_name_from_snippet_basic() -> None:
    """_extract_case_name_from_snippet returns the X v. Y name from surrounding text."""
    from app.services.search_fallback import _extract_case_name_from_snippet

    snippet = "In Marshall v. Amuso, 571 F. Supp. 3d 412 (E.D. Pa. 2021), the court held"
    result = _extract_case_name_from_snippet(snippet)
    assert result is not None
    assert "Marshall" in result
    assert "Amuso" in result


def test_extract_case_name_no_pattern() -> None:
    """_extract_case_name_from_snippet returns None when no X v. Y pattern exists."""
    from app.services.search_fallback import _extract_case_name_from_snippet

    snippet = "See 571 F. Supp. 3d 412 for damages."
    assert _extract_case_name_from_snippet(snippet) is None


def test_build_strategies_uses_snippet_case_name() -> None:
    """_build_strategies first strategy includes the case name extracted from the snippet."""
    from app.services.search_fallback import _build_strategies

    citation = CitationResult(
        raw_text="571 F. Supp. 3d 412",
        citation_type="FullCaseCitation",
        snippet="Marshall v. Amuso, 571 F. Supp. 3d 412 (E.D. Pa. 2021)",
    )
    strategies = _build_strategies(citation)
    assert strategies, "Should produce at least one strategy"
    first = strategies[0]
    assert "Marshall" in first
    assert "Amuso" in first


def test_search_fallback_too_many_results_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """try_search_fallback returns None (keeps NOT_FOUND) when total count > 3."""
    from app.services.search_fallback import try_search_fallback

    def fake_search(query, *, token, search_url, timeout_seconds):
        return {
            "count": 10,
            "results": [
                {
                    "cluster_id": i,
                    "caseName": f"Case {i}",
                    "court_id": "ca1",
                    "dateFiled": "2020-01-01",
                }
                for i in range(1, 6)
            ],
        }

    monkeypatch.setattr("app.services.search_fallback._search_courtlistener", fake_search)
    monkeypatch.setattr("app.services.search_fallback.time.sleep", lambda _: None)

    citation = CitationResult(
        raw_text="571 F. Supp. 3d 412",
        citation_type="FullCaseCitation",
        snippet="Marshall v. Amuso, 571 F. Supp. 3d 412 (E.D. Pa. 2021)",
    )
    result = try_search_fallback(citation, token="tok")
    assert result is None, "Should return None when search returns more than 3 results"


def test_search_fallback_two_results_returns_ambiguous(monkeypatch: pytest.MonkeyPatch) -> None:
    """try_search_fallback returns AMBIGUOUS when exactly 2 results are returned."""
    from app.services.search_fallback import try_search_fallback

    def fake_search(query, *, token, search_url, timeout_seconds):
        return {
            "count": 2,
            "results": [
                {
                    "cluster_id": 101,
                    "caseName": "Alpha v. Beta",
                    "court_id": "ca1",
                    "dateFiled": "2019-01-01",
                },
                {
                    "cluster_id": 202,
                    "caseName": "Alpha v. Beta",
                    "court_id": "ca2",
                    "dateFiled": "2021-01-01",
                },
            ],
        }

    monkeypatch.setattr("app.services.search_fallback._search_courtlistener", fake_search)
    monkeypatch.setattr("app.services.search_fallback.time.sleep", lambda _: None)

    citation = CitationResult(
        raw_text="Alpha v. Beta, 2019 LEXIS 123",
        citation_type="FullCaseCitation",
        snippet="Alpha v. Beta, 2019 LEXIS 123 (1st Cir. 2019)",
    )
    result = try_search_fallback(citation, token="tok")
    assert result is not None
    assert result.status == "AMBIGUOUS"
    assert result.candidate_cluster_ids is not None
    assert len(result.candidate_cluster_ids) == 2


def test_search_fallback_three_results_still_ambiguous(monkeypatch: pytest.MonkeyPatch) -> None:
    """try_search_fallback returns AMBIGUOUS for exactly 3 results (boundary)."""
    from app.services.search_fallback import try_search_fallback

    def fake_search(query, *, token, search_url, timeout_seconds):
        return {
            "count": 3,
            "results": [
                {
                    "cluster_id": i,
                    "caseName": f"Case {i}",
                    "court_id": "ca1",
                    "dateFiled": "2020-01-01",
                }
                for i in range(1, 4)
            ],
        }

    monkeypatch.setattr("app.services.search_fallback._search_courtlistener", fake_search)
    monkeypatch.setattr("app.services.search_fallback.time.sleep", lambda _: None)

    citation = CitationResult(
        raw_text="Alpha v. Beta, 2019 LEXIS 123",
        citation_type="FullCaseCitation",
        snippet="Alpha v. Beta, 2019 LEXIS 123 (1st Cir. 2019)",
    )
    result = try_search_fallback(citation, token="tok")
    assert result is not None
    assert result.status == "AMBIGUOUS"


def test_search_fallback_four_results_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """try_search_fallback returns None for 4 results (one above threshold)."""
    from app.services.search_fallback import try_search_fallback

    def fake_search(query, *, token, search_url, timeout_seconds):
        return {
            "count": 4,
            "results": [
                {
                    "cluster_id": i,
                    "caseName": f"Case {i}",
                    "court_id": "ca1",
                    "dateFiled": "2020-01-01",
                }
                for i in range(1, 5)
            ],
        }

    monkeypatch.setattr("app.services.search_fallback._search_courtlistener", fake_search)
    monkeypatch.setattr("app.services.search_fallback.time.sleep", lambda _: None)

    citation = CitationResult(
        raw_text="Alpha v. Beta, 2019 LEXIS 123",
        citation_type="FullCaseCitation",
        snippet="Alpha v. Beta, 2019 LEXIS 123 (1st Cir. 2019)",
    )
    result = try_search_fallback(citation, token="tok")
    assert result is None


# ── Issue B: Short citation matching ─────────────────────────────────────────


def test_parse_volume_reporter_short_cite() -> None:
    """_parse_volume_reporter extracts (volume, reporter) from short citations."""
    from app.services.verification import _parse_volume_reporter

    assert _parse_volume_reporter("588 U.S. at 392") == ("588", "u.s")
    assert _parse_volume_reporter("123 F.3d at 456") == ("123", "f.3d")


def test_parse_volume_reporter_full_cite() -> None:
    """_parse_volume_reporter extracts (volume, reporter) from full citations."""
    from app.services.verification import _parse_volume_reporter

    assert _parse_volume_reporter("588 U.S. 388") == ("588", "u.s")
    assert _parse_volume_reporter("123 F.3d 400 (1st Cir. 2001)") == ("123", "f.3d")


def test_parse_volume_reporter_non_citation() -> None:
    """_parse_volume_reporter returns None for non-citation text."""
    from app.services.verification import _parse_volume_reporter

    assert _parse_volume_reporter("not a citation") is None
    assert _parse_volume_reporter("id. at 5") is None


def test_short_cite_matches_verified_full_citation() -> None:
    """ShortCaseCitation resolves when a VERIFIED full citation shares volume+reporter."""

    class _VerifierStub:
        def verify(self, citation: CitationResult) -> VerificationResponse:
            if citation.raw_text == "588 U.S. 388":
                return VerificationResponse(status="VERIFIED", detail="Matched.")
            return VerificationResponse(status="NOT_FOUND", detail="No match.")

    from app.services.verification import verify_citations

    full_cite = CitationResult(raw_text="588 U.S. 388", citation_type="FullCaseCitation")
    short_cite = CitationResult(raw_text="588 U.S. at 392", citation_type="ShortCaseCitation")

    results = verify_citations(
        [full_cite, short_cite],
        courtlistener_token="tok",
        verification_base_url="http://test",
        verifier=_VerifierStub(),
        search_fallback_enabled=False,
    )

    full_result = next(r for r in results if r.raw_text == "588 U.S. 388")
    short_result = next(r for r in results if r.raw_text == "588 U.S. at 392")

    assert full_result.verification_status == "VERIFIED"
    assert short_result.verification_status == "VERIFIED"
    assert short_result.resolution_method == "short_cite_match"
    assert "588 U.S. 388" in short_result.verification_detail
    assert "same reporter and volume" in short_result.verification_detail


def test_short_cite_multiple_short_cites_same_full() -> None:
    """Multiple short cites to the same volume+reporter all resolve."""

    class _VerifierStub:
        def verify(self, citation: CitationResult) -> VerificationResponse:
            if citation.raw_text == "588 U.S. 388":
                return VerificationResponse(status="VERIFIED", detail="Matched.")
            return VerificationResponse(status="NOT_FOUND", detail="No match.")

    from app.services.verification import verify_citations

    full_cite = CitationResult(raw_text="588 U.S. 388", citation_type="FullCaseCitation")
    short1 = CitationResult(raw_text="588 U.S. at 392", citation_type="ShortCaseCitation")
    short2 = CitationResult(raw_text="588 U.S. at 395", citation_type="ShortCaseCitation")

    results = verify_citations(
        [full_cite, short1, short2],
        courtlistener_token="tok",
        verification_base_url="http://test",
        verifier=_VerifierStub(),
        search_fallback_enabled=False,
    )

    short_results = [r for r in results if r.citation_type == "ShortCaseCitation"]
    assert all(r.verification_status == "VERIFIED" for r in short_results)
    assert all(r.resolution_method == "short_cite_match" for r in short_results)


def test_short_cite_no_matching_full_citation_stays_unresolved() -> None:
    """ShortCaseCitation stays NOT_FOUND when no VERIFIED full citation matches."""

    class _NotFoundVerifier:
        def verify(self, citation: CitationResult) -> VerificationResponse:
            return VerificationResponse(status="NOT_FOUND", detail="No match.")

    from app.services.verification import verify_citations

    short_cite = CitationResult(raw_text="999 F.3d at 100", citation_type="ShortCaseCitation")

    results = verify_citations(
        [short_cite],
        courtlistener_token="tok",
        verification_base_url="http://test",
        verifier=_NotFoundVerifier(),
        search_fallback_enabled=False,
    )

    assert results[0].verification_status == "NOT_FOUND"
    assert results[0].resolution_method is None


def test_short_cite_different_volume_not_matched() -> None:
    """ShortCaseCitation with different volume does NOT match a VERIFIED full citation."""

    class _VerifierStub:
        def verify(self, citation: CitationResult) -> VerificationResponse:
            if citation.raw_text == "588 U.S. 388":
                return VerificationResponse(status="VERIFIED", detail="Matched.")
            return VerificationResponse(status="NOT_FOUND", detail="No match.")

    from app.services.verification import verify_citations

    full_cite = CitationResult(raw_text="588 U.S. 388", citation_type="FullCaseCitation")
    short_cite = CitationResult(raw_text="590 U.S. at 100", citation_type="ShortCaseCitation")

    results = verify_citations(
        [full_cite, short_cite],
        courtlistener_token="tok",
        verification_base_url="http://test",
        verifier=_VerifierStub(),
        search_fallback_enabled=False,
    )

    short_result = next(r for r in results if r.raw_text == "590 U.S. at 100")
    assert short_result.verification_status == "NOT_FOUND"
    assert short_result.resolution_method is None


def test_short_cite_ambiguous_resolved_by_short_cite_match() -> None:
    """ShortCaseCitation that is AMBIGUOUS is also resolved by short-cite match."""

    class _VerifierStub:
        def verify(self, citation: CitationResult) -> VerificationResponse:
            if citation.raw_text == "588 U.S. 388":
                return VerificationResponse(status="VERIFIED", detail="Matched.")
            # Short cite comes back AMBIGUOUS from CourtListener
            return VerificationResponse(
                status="AMBIGUOUS",
                detail="Multiple matches.",
                candidate_cluster_ids=[1, 2],
                candidate_metadata=[
                    {
                        "cluster_id": 1,
                        "case_name": "A v. B",
                        "court": "scotus",
                        "date_filed": "2021-01-01",
                    },
                    {
                        "cluster_id": 2,
                        "case_name": "A v. B",
                        "court": "scotus",
                        "date_filed": "2022-01-01",
                    },
                ],
            )

    from app.services.verification import verify_citations

    full_cite = CitationResult(raw_text="588 U.S. 388", citation_type="FullCaseCitation")
    short_cite = CitationResult(raw_text="588 U.S. at 392", citation_type="ShortCaseCitation")

    results = verify_citations(
        [full_cite, short_cite],
        courtlistener_token="tok",
        verification_base_url="http://test",
        verifier=_VerifierStub(),
        search_fallback_enabled=False,
    )

    short_result = next(r for r in results if r.raw_text == "588 U.S. at 392")
    assert short_result.verification_status == "VERIFIED"
    assert short_result.resolution_method == "short_cite_match"


# ── Issue C: Clickable status filter UI ──────────────────────────────────────


def test_dashboard_summary_cards_have_filter_attributes() -> None:
    """After an audit, dashboard summary cards have data-filter-status attributes."""
    response = client.post(
        "/audit", data={"pasted_text": "See Brown v. Board, 347 U.S. 483 (1954)."}
    )
    assert response.status_code == 200
    assert "data-filter-status=" in response.text


def test_dashboard_citation_items_have_data_status() -> None:
    """Citation list items on the dashboard carry data-status attributes."""
    response = client.post(
        "/audit", data={"pasted_text": "See Brown v. Board, 347 U.S. 483 (1954)."}
    )
    assert response.status_code == 200
    assert 'data-status="' in response.text


def test_dashboard_filter_note_elements_present() -> None:
    """Dashboard renders a filter-note element for each result group."""
    response = client.post(
        "/audit", data={"pasted_text": "See Brown v. Board, 347 U.S. 483 (1954)."}
    )
    assert response.status_code == 200
    assert "filter-note-" in response.text


def test_history_detail_summary_cards_have_filter_attributes() -> None:
    """History detail page summary cards have data-filter-status attributes."""
    client.post("/audit", data={"pasted_text": "Brown v. Board, 347 U.S. 483 (1954)."})

    history_resp = client.get("/history")
    assert history_resp.status_code == 200
    run_ids = re.findall(r"/history/(\d+)", history_resp.text)
    assert run_ids, "Expected at least one run in history"

    detail_resp = client.get(f"/history/{run_ids[0]}")
    assert detail_resp.status_code == 200
    assert "data-filter-status=" in detail_resp.text


def test_history_detail_citation_items_have_data_status() -> None:
    """Citation items on history detail page carry data-status attributes."""
    client.post("/audit", data={"pasted_text": "Brown v. Board, 347 U.S. 483 (1954)."})

    history_resp = client.get("/history")
    run_ids = re.findall(r"/history/(\d+)", history_resp.text)
    assert run_ids

    detail_resp = client.get(f"/history/{run_ids[0]}")
    assert detail_resp.status_code == 200
    assert 'class="citation-item" data-status="' in detail_resp.text


def test_history_detail_filter_js_present() -> None:
    """History detail page has the status-filter JavaScript."""
    client.post("/audit", data={"pasted_text": "Brown v. Board, 347 U.S. 483 (1954)."})

    history_resp = client.get("/history")
    run_ids = re.findall(r"/history/(\d+)", history_resp.text)
    assert run_ids

    detail_resp = client.get(f"/history/{run_ids[0]}")
    assert detail_resp.status_code == 200
    assert "history-summary-grid" in detail_resp.text
    assert "applyStatusFilter" in detail_resp.text


def test_dashboard_filter_js_present() -> None:
    """Dashboard page has the per-group status-filter JavaScript."""
    response = client.post(
        "/audit", data={"pasted_text": "See Brown v. Board, 347 U.S. 483 (1954)."}
    )
    assert response.status_code == 200
    assert "applyGroupFilter" in response.text
    assert "summary-card-active" in response.text
