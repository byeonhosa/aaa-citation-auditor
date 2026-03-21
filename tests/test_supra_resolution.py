"""Tests for supra citation resolution.

Supra citations (e.g. "Brown, supra") must:
1. Be pre-resolved to the earlier full citation they reference.
2. Never be sent to CourtListener.
3. Never be cached under the bare key "supra," or "supra.".
4. Be marked DERIVED (with resolution_method="supra_ref") when matched.
5. Be marked AMBIGUOUS when the antecedent cannot be matched.
"""

from __future__ import annotations

from app.services.audit import (
    CitationResult,
    extract_citations,
    resolve_id_citations,
    resolve_supra_citations,
)
from app.services.verification import verify_citations

# ── resolve_supra_citations unit tests ────────────────────────────────────────


def _make_full(raw: str, snippet: str | None = None) -> CitationResult:
    return CitationResult(
        raw_text=raw,
        citation_type="FullCaseCitation",
        snippet=snippet or raw,
    )


def _make_supra(antecedent: str) -> CitationResult:
    return CitationResult(
        raw_text="supra,",
        citation_type="SupraCitation",
        normalized_text="supra,",
        antecedent_guess=antecedent,
    )


def test_supra_resolves_to_matching_earlier_citation() -> None:
    """'Brown, supra' links to the Brown v. Board citation cited earlier."""
    brown = _make_full(
        "347 U.S. 483",
        snippet="Brown v. Board of Education, 347 U.S. 483 (1954).",
    )
    supra = _make_supra("Brown")

    result = resolve_supra_citations([brown, supra])

    assert result[1].resolved_from == "347 U.S. 483"


def test_supra_resolves_to_correct_case_when_multiple_prior_citations() -> None:
    """'Robinson, supra' resolves to Robinson, not Brown, when both are cited."""
    brown = _make_full(
        "347 U.S. 483",
        snippet="Brown v. Board of Education, 347 U.S. 483 (1954).",
    )
    robinson = _make_full(
        "300 U.S. 100",
        snippet="Robinson v. United States, 300 U.S. 100 (1937).",
    )
    supra_robinson = _make_supra("Robinson")

    result = resolve_supra_citations([brown, robinson, supra_robinson])

    assert result[2].resolved_from == "300 U.S. 100"


def test_supra_not_resolved_when_no_matching_citation() -> None:
    """Supra with no matching antecedent leaves resolved_from as None."""
    unrelated = _make_full("500 U.S. 1", snippet="Smith v. Jones, 500 U.S. 1 (2000).")
    supra = _make_supra("Brown")

    result = resolve_supra_citations([unrelated, supra])

    assert result[1].resolved_from is None


def test_supra_without_antecedent_leaves_resolved_from_none() -> None:
    """SupraCitation with no antecedent_guess is left unresolved."""
    brown = _make_full("347 U.S. 483", snippet="Brown v. Board of Education.")
    supra = CitationResult(
        raw_text="supra,",
        citation_type="SupraCitation",
        antecedent_guess=None,
    )

    result = resolve_supra_citations([brown, supra])

    assert result[1].resolved_from is None


def test_supra_prefers_most_recent_matching_citation() -> None:
    """When the antecedent matches two citations, the later one is chosen."""
    brown1 = _make_full("200 U.S. 1", snippet="Brown v. Allen, 200 U.S. 1 (1920).")
    brown2 = _make_full("347 U.S. 483", snippet="Brown v. Board of Education, 347 U.S. 483.")
    supra = _make_supra("Brown")

    result = resolve_supra_citations([brown1, brown2, supra])

    assert result[2].resolved_from == "347 U.S. 483"


# ── Integration: extract_citations captures antecedent_guess ─────────────────


def test_extract_citations_captures_antecedent_guess() -> None:
    """extract_citations populates antecedent_guess from eyecite metadata."""
    text = "See Brown v. Board of Education, 347 U.S. 483 (1954). See also Brown, supra, at 490."
    results, _ = extract_citations(text)

    supra_cites = [r for r in results if r.citation_type == "SupraCitation"]
    assert len(supra_cites) == 1
    assert supra_cites[0].antecedent_guess == "Brown"


# ── Integration: verify_citations handles supra correctly ─────────────────────


def test_verify_supra_matched_marked_derived() -> None:
    """Pre-resolved supra (resolved_from set) is marked DERIVED, not sent to CL."""
    full = _make_full("347 U.S. 483", snippet="Brown v. Board of Education, 347 U.S. 483.")
    full.verification_status = "VERIFIED"
    supra = _make_supra("Brown")
    supra.resolved_from = "347 U.S. 483"

    verified = verify_citations(
        [full, supra],
        courtlistener_token="token",
        verification_base_url="https://example.test/verify",
    )

    supra_result = verified[1]
    assert supra_result.verification_status == "DERIVED"
    assert supra_result.resolution_method == "supra_ref"


def test_verify_supra_unmatched_marked_ambiguous() -> None:
    """Unresolved supra (no matching antecedent) is marked AMBIGUOUS."""
    supra = _make_supra("Brown")
    # resolved_from is None — no match was found

    verified = verify_citations(
        [supra],
        courtlistener_token="token",
        verification_base_url="https://example.test/verify",
    )

    assert verified[0].verification_status == "AMBIGUOUS"
    assert "Brown" in (verified[0].verification_detail or "")


def test_supra_never_sent_to_courtlistener(monkeypatch) -> None:
    """SupraCitation must not reach the CourtListener verifier."""
    called = []

    class SpyVerifier:
        def verify(self, citations, base_url, token, timeout):
            called.extend(citations)
            return []

    supra = _make_supra("Brown")
    supra.resolved_from = "347 U.S. 483"  # pre-resolved

    verify_citations(
        [supra],
        courtlistener_token="token",
        verification_base_url="https://example.test/verify",
        verifier=SpyVerifier(),
    )

    # The spy verifier should never have been called for a supra citation.
    assert not called, "SupraCitation was incorrectly sent to CourtListener"


def test_supra_not_cached_under_supra_key(monkeypatch) -> None:
    """Supra citations must not hit or populate the cache under key 'supra,'."""
    stale_cache = {"supra,": {"cluster_id": 112893, "case_name": "Brooke Group"}}

    supra = _make_supra("Brown")
    # Even with a stale cache entry, supra should NOT be VERIFIED from cache.

    verified = verify_citations(
        [supra],
        courtlistener_token="token",
        verification_base_url="https://example.test/verify",
        resolution_cache=stale_cache,
    )

    # Should be AMBIGUOUS (no resolved_from), not VERIFIED via stale cache.
    assert verified[0].verification_status != "VERIFIED"
    assert verified[0].selected_cluster_id != 112893


# ── End-to-end: full pipeline with real eyecite extraction ────────────────────


def test_full_pipeline_brown_supra_resolves_to_brown_not_brooke_group() -> None:
    """End-to-end: 'Brown, supra' resolves to Brown v. Board, not Brooke Group."""
    text = (
        "See Brown v. Board of Education, 347 U.S. 483 (1954). "
        "See Robinson v. United States, 300 U.S. 100 (1937). "
        "See Brown, supra, at 490. "
        "See Robinson, supra."
    )
    citations, _ = extract_citations(text)
    citations = resolve_id_citations(citations)
    citations = resolve_supra_citations(citations)

    supra_results = [c for c in citations if c.citation_type == "SupraCitation"]
    assert len(supra_results) == 2

    brown_supra, robinson_supra = supra_results

    # Brown supra should link to the Brown citation (raw_text "347 U.S. 483")
    assert brown_supra.resolved_from == "347 U.S. 483"
    assert brown_supra.antecedent_guess == "Brown"

    # Robinson supra should link to the Robinson citation, not Brown
    assert robinson_supra.resolved_from == "300 U.S. 100"
    assert robinson_supra.antecedent_guess == "Robinson"


def test_full_pipeline_supra_with_no_antecedent_match_is_ambiguous() -> None:
    """Supra with no matching prior citation is AMBIGUOUS after verification."""
    text = (
        "See Smith v. Jones, 500 U.S. 1 (2000). "
        "See Brown, supra."  # Brown was never cited
    )
    citations, _ = extract_citations(text)
    citations = resolve_id_citations(citations)
    citations = resolve_supra_citations(citations)

    verified = verify_citations(
        citations,
        courtlistener_token=None,  # no token needed — supra handled before CL
        verification_base_url="https://example.test/verify",
    )

    supra_results = [c for c in verified if c.citation_type == "SupraCitation"]
    assert len(supra_results) == 1
    assert supra_results[0].verification_status == "AMBIGUOUS"
