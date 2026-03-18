"""Tests for parallel citation resolution."""

from __future__ import annotations

from app.services.audit import CitationResult
from app.services.verification import _is_parallel_adjacent, _resolve_parallel_citations


def _make_citation(
    raw_text: str,
    status: str,
    snippet: str | None = None,
    cluster_id: int | None = None,
    resolution_method: str | None = None,
) -> CitationResult:
    c = CitationResult(raw_text=raw_text, citation_type="FullCaseCitation")
    c.verification_status = status
    c.snippet = snippet
    c.selected_cluster_id = cluster_id
    c.resolution_method = resolution_method
    if cluster_id is not None:
        c.candidate_cluster_ids = [cluster_id]
        c.candidate_metadata = [
            {
                "cluster_id": cluster_id,
                "case_name": "Test v. Case",
                "court": "va",
                "date_filed": "2010-01-01",
            }
        ]
    return c


class TestIsParallelAdjacent:
    def test_adjacent_with_comma(self):
        snippet = "See 283 Va. 474, 722 S.E.2d 272 (2012)."
        assert _is_parallel_adjacent("283 Va. 474", "722 S.E.2d 272", snippet) is True

    def test_adjacent_reversed_order(self):
        snippet = "See 283 Va. 474, 722 S.E.2d 272 (2012)."
        assert _is_parallel_adjacent("722 S.E.2d 272", "283 Va. 474", snippet) is True

    def test_not_adjacent_with_word_between(self):
        snippet = "See 100 Va. 200 and 200 F.3d 100 for contrast."
        assert _is_parallel_adjacent("100 Va. 200", "200 F.3d 100", snippet) is False

    def test_not_adjacent_far_apart(self):
        snippet = "See 100 Va. 200. Many pages later, 200 F.3d 100 is cited."
        assert _is_parallel_adjacent("100 Va. 200", "200 F.3d 100", snippet) is False

    def test_not_in_snippet(self):
        snippet = "Some other text entirely."
        assert _is_parallel_adjacent("283 Va. 474", "722 S.E.2d 272", snippet) is False

    def test_adjacent_space_only(self):
        snippet = "283 Va. 474 722 S.E.2d 272"
        assert _is_parallel_adjacent("283 Va. 474", "722 S.E.2d 272", snippet) is True


class TestResolveParallelCitations:
    def test_not_found_resolved_from_adjacent_verified(self):
        verified = _make_citation(
            "283 Va. 474",
            "VERIFIED",
            snippet="See 283 Va. 474, 722 S.E.2d 272 (2012).",
            cluster_id=12345,
        )
        not_found = _make_citation(
            "722 S.E.2d 272",
            "NOT_FOUND",
            snippet="See 283 Va. 474, 722 S.E.2d 272 (2012).",
        )
        count = _resolve_parallel_citations([verified, not_found])
        assert count == 1
        assert not_found.verification_status == "VERIFIED"
        assert not_found.selected_cluster_id == 12345
        assert not_found.resolution_method == "parallel_cite"
        assert "283 Va. 474" in not_found.verification_detail

    def test_ambiguous_resolved_from_adjacent_verified(self):
        verified = _make_citation(
            "100 Va. 200",
            "VERIFIED",
            snippet="100 Va. 200, 50 S.E.2d 100 (1950).",
            cluster_id=99,
        )
        ambiguous = _make_citation(
            "50 S.E.2d 100",
            "AMBIGUOUS",
            snippet="100 Va. 200, 50 S.E.2d 100 (1950).",
        )
        count = _resolve_parallel_citations([verified, ambiguous])
        assert count == 1
        assert ambiguous.verification_status == "VERIFIED"
        assert ambiguous.resolution_method == "parallel_cite"

    def test_no_adjacent_not_resolved(self):
        verified = _make_citation(
            "100 Va. 200",
            "VERIFIED",
            snippet="Citing 100 Va. 200 for proposition A.",
            cluster_id=1,
        )
        not_found = _make_citation(
            "200 F.3d 100",
            "NOT_FOUND",
            snippet="Citing 200 F.3d 100 for proposition B.",
        )
        count = _resolve_parallel_citations([verified, not_found])
        assert count == 0
        assert not_found.verification_status == "NOT_FOUND"

    def test_already_verified_not_changed(self):
        c1 = _make_citation(
            "100 Va. 200",
            "VERIFIED",
            snippet="100 Va. 200, 50 S.E.2d 100.",
            cluster_id=1,
        )
        c2 = _make_citation(
            "50 S.E.2d 100",
            "VERIFIED",
            snippet="100 Va. 200, 50 S.E.2d 100.",
            cluster_id=2,
        )
        count = _resolve_parallel_citations([c1, c2])
        assert count == 0

    def test_statute_citations_excluded(self):
        """FullLawCitation (statute) should not be linked via parallel resolution."""
        verified = _make_citation(
            "100 Va. 200",
            "VERIFIED",
            snippet="100 Va. 200, 42 U.S.C. § 1983.",
            cluster_id=1,
        )
        statute = CitationResult(raw_text="42 U.S.C. § 1983", citation_type="FullLawCitation")
        statute.verification_status = "NOT_FOUND"
        statute.snippet = "100 Va. 200, 42 U.S.C. § 1983."
        count = _resolve_parallel_citations([verified, statute])
        assert count == 0
