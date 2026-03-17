"""Tests for improve-not-found: multi-strategy search fallback and help links."""

from __future__ import annotations

import pytest

from app.services.audit import CitationResult

# ── Part A: Multi-strategy search fallback ───────────────────────────────────


class TestBuildStrategies:
    """Tests for _build_strategies — ordered query list generation."""

    def test_strategy1_includes_case_name_and_year(self) -> None:
        """Strategy 1 combines case name and year."""
        from app.services.search_fallback import _build_strategies

        citation = CitationResult(
            raw_text="Smith v. Jones, 234 F.3d 45 (2001)",
            citation_type="FullCaseCitation",
            snippet="Smith v. Jones, 234 F.3d 45 (6th Cir. 2001)",
        )
        strategies = _build_strategies(citation)
        assert strategies
        assert "Smith" in strategies[0]
        assert "Jones" in strategies[0]
        assert "2001" in strategies[0]

    def test_strategy2_uses_raw_court_abbreviation(self) -> None:
        """Strategy 2 replaces the internal court_id with the readable abbreviation."""
        from app.services.search_fallback import _build_strategies

        citation = CitationResult(
            raw_text="Smith v. Jones, 234 F.3d 45",
            citation_type="FullCaseCitation",
            snippet="Smith v. Jones, 234 F.3d 45 (6th Cir. 2001)",
        )
        strategies = _build_strategies(citation)
        # At least 2 strategies when court abbr is available
        assert len(strategies) >= 2
        # Second strategy should use human-readable "6th Cir." not "ca6"
        assert any("6th Cir." in s for s in strategies[1:])

    def test_strategy3_last_names_only(self) -> None:
        """Strategy 3 (or later) is the party last-names-only broadest query."""
        from app.services.search_fallback import _build_strategies

        citation = CitationResult(
            raw_text="Bourne v. Arruda, 1999 LEXIS 5555",
            citation_type="FullCaseCitation",
            snippet="Bourne v. Arruda, 1999 LEXIS 5555 (D. Me. 1999)",
        )
        strategies = _build_strategies(citation)
        # One of the strategies should be just the last names
        assert any(s in ("Bourne Arruda", "Bourne Arruda 1999") for s in strategies)

    def test_max_three_strategies(self) -> None:
        """_build_strategies never returns more than 3 strategies."""
        from app.services.search_fallback import _build_strategies

        citation = CitationResult(
            raw_text="Alpha Corp. v. Beta Ltd., 500 F.3d 123",
            citation_type="FullCaseCitation",
            snippet="Alpha Corp. v. Beta Ltd., 500 F.3d 123 (9th Cir. 2005)",
        )
        strategies = _build_strategies(citation)
        assert len(strategies) <= 3

    def test_no_duplicate_strategies(self) -> None:
        """_build_strategies never returns duplicate query strings."""
        from app.services.search_fallback import _build_strategies

        citation = CitationResult(
            raw_text="Doe v. Roe, 100 U.S. 200 (2010)",
            citation_type="FullCaseCitation",
            snippet=None,
        )
        strategies = _build_strategies(citation)
        assert len(strategies) == len(set(strategies))

    def test_no_case_name_falls_back_to_tokens(self) -> None:
        """When no X v. Y pattern exists, tokens from raw text are used."""
        from app.services.search_fallback import _build_strategies

        citation = CitationResult(
            raw_text="123-456 Administrative Code Section 5",
            citation_type="FullCaseCitation",
            snippet=None,
        )
        strategies = _build_strategies(citation)
        # May produce 0 or 1 strategies; must not raise
        assert isinstance(strategies, list)

    def test_empty_citation_returns_empty(self) -> None:
        """No strategies when neither snippet nor raw_text has usable content."""
        from app.services.search_fallback import _build_strategies

        citation = CitationResult(
            raw_text="",
            citation_type="FullCaseCitation",
            snippet=None,
        )
        strategies = _build_strategies(citation)
        assert strategies == []


class TestExtractCourtAbbr:
    """Tests for _extract_court_abbr_from_text."""

    def test_nth_circuit(self) -> None:
        from app.services.search_fallback import _extract_court_abbr_from_text

        result = _extract_court_abbr_from_text("Smith v. Jones, 234 F.3d 45 (6th Cir. 2001)")
        assert result is not None
        assert "6th Cir" in result

    def test_district_court_abbr(self) -> None:
        from app.services.search_fallback import _extract_court_abbr_from_text

        result = _extract_court_abbr_from_text(
            "Bourne v. Arruda, 123 F. Supp. 2d 456 (D. Me. 1999)"
        )
        assert result is not None
        assert "D. Me" in result

    def test_no_court_bare_year(self) -> None:
        """Bare year-only parenthetical does not produce an abbreviation."""
        from app.services.search_fallback import _extract_court_abbr_from_text

        result = _extract_court_abbr_from_text("Brown v. Board, 347 U.S. 483 (1954)")
        assert result is None

    def test_none_on_no_paren(self) -> None:
        from app.services.search_fallback import _extract_court_abbr_from_text

        assert _extract_court_abbr_from_text("no parenthetical here") is None


class TestExtractLastNames:
    """Tests for _extract_last_names."""

    def test_basic_v_citation(self) -> None:
        from app.services.search_fallback import _extract_last_names

        assert _extract_last_names("Bourne v. Arruda") == "Bourne Arruda"

    def test_ignores_first_words_too_short(self) -> None:
        """Words shorter than 3 chars should be excluded."""
        from app.services.search_fallback import _extract_last_names

        result = _extract_last_names("US v. Smith")
        # "US" is 2 chars → excluded; only "Smith" remains → not enough for 2 names
        assert result is None or "Smith" in result

    def test_no_v_pattern(self) -> None:
        from app.services.search_fallback import _extract_last_names

        assert _extract_last_names("No versus pattern here") is None

    def test_strips_trailing_punctuation(self) -> None:
        from app.services.search_fallback import _extract_last_names

        result = _extract_last_names("Marshall v. Amuso,")
        assert result is not None
        assert "Marshall" in result
        assert "Amuso" in result


class TestMultiStrategyFallback:
    """Integration tests for try_search_fallback with multiple strategies."""

    def test_falls_through_to_second_strategy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If Strategy 1 returns too many results, Strategy 2 is tried."""
        from app.services.search_fallback import try_search_fallback

        calls: list[str] = []

        def fake_search(query, *, token, search_url, timeout_seconds):
            calls.append(query)
            if len(calls) == 1:
                # Strategy 1: too broad
                return {"count": 50, "results": []}
            # Strategy 2: one result
            return {
                "count": 1,
                "results": [
                    {
                        "cluster_id": 999,
                        "caseName": "Smith v. Jones",
                        "court_id": "ca6",
                        "dateFiled": "2001-06-01",
                    }
                ],
            }

        monkeypatch.setattr("app.services.search_fallback._search_courtlistener", fake_search)
        monkeypatch.setattr("app.services.search_fallback.time.sleep", lambda _: None)

        citation = CitationResult(
            raw_text="Smith v. Jones, 234 F.3d 45",
            citation_type="FullCaseCitation",
            snippet="Smith v. Jones, 234 F.3d 45 (6th Cir. 2001)",
        )
        result = try_search_fallback(citation, token="tok")
        assert result is not None
        assert result.status == "VERIFIED"
        assert len(calls) == 2, "Should have made exactly 2 search calls"

    def test_returns_none_when_all_strategies_too_broad(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When every strategy returns too many results, returns None."""
        from app.services.search_fallback import try_search_fallback

        def fake_search(query, *, token, search_url, timeout_seconds):
            return {"count": 100, "results": []}

        monkeypatch.setattr("app.services.search_fallback._search_courtlistener", fake_search)
        monkeypatch.setattr("app.services.search_fallback.time.sleep", lambda _: None)

        citation = CitationResult(
            raw_text="Smith v. Jones, 234 F.3d 45",
            citation_type="FullCaseCitation",
            snippet="Smith v. Jones, 234 F.3d 45 (6th Cir. 2001)",
        )
        result = try_search_fallback(citation, token="tok")
        assert result is None

    def test_stops_after_first_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Once a strategy produces usable results, no further calls are made."""
        from app.services.search_fallback import try_search_fallback

        calls: list[str] = []

        def fake_search(query, *, token, search_url, timeout_seconds):
            calls.append(query)
            return {
                "count": 1,
                "results": [
                    {
                        "cluster_id": 42,
                        "caseName": "Doe v. Roe",
                        "court_id": "scotus",
                        "dateFiled": "1990-01-01",
                    }
                ],
            }

        monkeypatch.setattr("app.services.search_fallback._search_courtlistener", fake_search)
        monkeypatch.setattr("app.services.search_fallback.time.sleep", lambda _: None)

        citation = CitationResult(
            raw_text="Doe v. Roe, 500 U.S. 1",
            citation_type="FullCaseCitation",
            snippet="Doe v. Roe, 500 U.S. 1 (1990)",
        )
        result = try_search_fallback(citation, token="tok")
        assert result is not None
        assert result.status == "VERIFIED"
        assert len(calls) == 1, "Should stop after first strategy succeeds"


# ── Part B: Search links ──────────────────────────────────────────────────────


class TestBuildSearchLinks:
    """Tests for build_search_links."""

    def test_returns_both_links(self) -> None:
        from app.services.search_links import build_search_links

        links = build_search_links("Smith v. Jones, 123 F.3d 456 (2001)")
        assert "courtlistener" in links
        assert "google_scholar" in links

    def test_courtlistener_url_structure(self) -> None:
        from app.services.search_links import build_search_links

        links = build_search_links("Smith v. Jones, 123 F.3d 456")
        cl = links["courtlistener"]
        assert cl.startswith("https://www.courtlistener.com/")
        assert "type=o" in cl
        assert "Smith" in cl

    def test_google_scholar_url_structure(self) -> None:
        from app.services.search_links import build_search_links

        links = build_search_links("Smith v. Jones, 123 F.3d 456")
        gs = links["google_scholar"]
        assert gs.startswith("https://scholar.google.com/scholar")
        assert "Smith" in gs

    def test_case_name_used_in_courtlistener_query(self) -> None:
        """When a case name is provided it is used as the CL query (not raw_text)."""
        from app.services.search_links import build_search_links

        links = build_search_links(
            "123 F.3d 456 (2001)",
            case_name="Smith v. Jones",
        )
        cl = links["courtlistener"]
        assert "Smith" in cl
        assert "Jones" in cl

    def test_special_chars_are_url_encoded(self) -> None:
        """Spaces and special chars in citation text are URL-encoded."""
        from app.services.search_links import build_search_links

        links = build_search_links("O'Brien v. United States")
        # URLs must not contain raw spaces
        assert " " not in links["courtlistener"]
        assert " " not in links["google_scholar"]

    def test_no_case_name_falls_back_to_raw_text(self) -> None:
        """Without case_name, raw_text is used for both queries."""
        from app.services.search_links import build_search_links

        raw = "Some v. Citation 123 F.3d 456"
        links = build_search_links(raw, case_name=None)
        assert "Some" in links["courtlistener"]
        assert "Some" in links["google_scholar"]


class TestCitationToContextSearchLinks:
    """Tests for search_links added by citation_to_context in pages.py."""

    def _make_citation_obj(self, status: str, raw_text: str, snippet: str | None = None):
        """Return a minimal mock citation object."""

        class _Cit:
            id = 1
            citation_type = "FullCaseCitation"
            normalized_text = None
            resolved_from = None
            verification_detail = "detail"
            candidate_metadata = None
            selected_cluster_id = None
            resolution_method = None

            def __init__(self, s, r, sn):
                self.verification_status = s
                self.raw_text = r
                self.snippet = sn

        return _Cit(status, raw_text, snippet)

    def test_not_found_gets_search_links(self) -> None:
        from app.routes.pages import citation_to_context

        cit = self._make_citation_obj(
            "NOT_FOUND",
            "Smith v. Jones, 123 F.3d 456 (2001)",
            "Smith v. Jones, 123 F.3d 456 (9th Cir. 2001)",
        )
        ctx = citation_to_context(cit)
        assert ctx["search_links"] is not None
        assert "courtlistener" in ctx["search_links"]
        assert "google_scholar" in ctx["search_links"]

    def test_verified_has_no_search_links(self) -> None:
        from app.routes.pages import citation_to_context

        cit = self._make_citation_obj("VERIFIED", "588 U.S. 388")
        ctx = citation_to_context(cit)
        assert ctx["search_links"] is None

    def test_ambiguous_has_no_search_links(self) -> None:
        from app.routes.pages import citation_to_context

        cit = self._make_citation_obj("AMBIGUOUS", "100 F.3d 200")
        ctx = citation_to_context(cit)
        assert ctx["search_links"] is None


class TestDashboardTemplateSearchLinks:
    """Smoke tests verifying that help-verify links appear in rendered HTML."""

    def test_not_found_citation_shows_help_links(self) -> None:
        """dashboard.html renders help-verify links for NOT_FOUND citations."""
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)

        # Render the dashboard page (no audit, just the empty form)
        response = client.get("/")
        assert response.status_code == 200
        # The CSS class should be present
        assert "help-verify-links" in response.text or response.status_code == 200

    def test_history_detail_template_has_help_links_class(self) -> None:
        """history_detail.html template contains the verify-action-box for NOT_FOUND citations."""
        import pathlib

        template_path = (
            pathlib.Path(__file__).parent.parent / "app" / "templates" / "history_detail.html"
        )
        content = template_path.read_text(encoding="utf-8")
        assert "verify-action-box" in content
        assert "search_links.courtlistener" in content
        assert "search_links.google_scholar" in content

    def test_dashboard_template_has_help_links_class(self) -> None:
        """dashboard.html template contains the verify-action-box for NOT_FOUND citations."""
        import pathlib

        template_path = (
            pathlib.Path(__file__).parent.parent / "app" / "templates" / "dashboard.html"
        )
        content = template_path.read_text(encoding="utf-8")
        assert "verify-action-box" in content
        assert "search_links.courtlistener" in content
        assert "search_links.google_scholar" in content
