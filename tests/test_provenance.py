"""Tests for app.services.provenance — provenance label mapping and helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.provenance import (
    PROVENANCE_HELP,
    ProvenanceInfo,
    get_provenance,
    get_provenance_breakdown,
)

# ── get_provenance: VERIFIED citations ────────────────────────────────────────


def test_verified_none_method_is_direct_match():
    info = get_provenance("VERIFIED", None)
    assert info.label == "Direct Match"
    assert info.css_class == "provenance-direct"


def test_verified_direct_method():
    info = get_provenance("VERIFIED", "direct")
    assert info.label == "Direct Match"
    assert info.css_class == "provenance-direct"


def test_verified_dedup_method_is_direct_match():
    info = get_provenance("VERIFIED", "dedup")
    assert info.label == "Direct Match"
    assert info.css_class == "provenance-direct"


def test_verified_heuristic_method():
    info = get_provenance("VERIFIED", "heuristic")
    assert info.label == "Heuristic Match"
    assert info.css_class == "provenance-heuristic"


def test_verified_user_method():
    info = get_provenance("VERIFIED", "user")
    assert info.label == "User Confirmed"
    assert info.css_class == "provenance-user"


def test_verified_short_cite_method():
    info = get_provenance("VERIFIED", "short_cite_match")
    assert info.label == "Short Citation Match"
    assert info.css_class == "provenance-short-cite"


def test_verified_search_fallback_method():
    info = get_provenance("VERIFIED", "search_fallback")
    assert info.label == "Search Match"
    assert info.css_class == "provenance-search"


def test_verified_unknown_method_falls_back_to_direct():
    info = get_provenance("VERIFIED", "totally_unknown_method")
    assert info.label == "Direct Match"


# ── get_provenance: cached citations ──────────────────────────────────────────


def test_verified_cache_no_original_method_is_direct_cached():
    info = get_provenance("VERIFIED", "cache")
    assert info.label == "Direct Match (cached)"
    assert "cached" in info.css_class


def test_verified_cache_with_direct_original():
    info = get_provenance("VERIFIED", "cache", original_method="direct")
    assert info.label == "Direct Match (cached)"
    assert info.css_class == "provenance-direct-cached"


def test_verified_cache_with_heuristic_original():
    info = get_provenance("VERIFIED", "cache", original_method="heuristic")
    assert info.label == "Heuristic Match (cached)"
    assert info.css_class == "provenance-heuristic-cached"


def test_verified_cache_with_user_original():
    info = get_provenance("VERIFIED", "cache", original_method="user")
    assert info.label == "User Confirmed (cached)"
    assert info.css_class == "provenance-user-cached"


def test_verified_cache_with_search_fallback_original():
    info = get_provenance("VERIFIED", "cache", original_method="search_fallback")
    assert info.label == "Search Match (cached)"
    assert info.css_class == "provenance-search-cached"


def test_verified_cache_original_method_is_cache_uses_direct():
    # original_method == "cache" is treated the same as None (avoid infinite loops)
    info = get_provenance("VERIFIED", "cache", original_method="cache")
    assert info.label == "Direct Match (cached)"


def test_verified_cache_description_mentions_cache():
    info = get_provenance("VERIFIED", "cache", original_method="heuristic")
    assert "cache" in info.description.lower()


# ── get_provenance: non-VERIFIED statuses ─────────────────────────────────────


def test_ambiguous_status():
    info = get_provenance("AMBIGUOUS", None)
    assert "Multiple" in info.label
    assert info.css_class == "provenance-ambiguous"


def test_not_found_status():
    info = get_provenance("NOT_FOUND", None)
    assert info.label == "Not Found"
    assert info.css_class == "provenance-not-found"


def test_derived_status():
    info = get_provenance("DERIVED", None)
    assert info.label == "Derived"
    assert info.css_class == "provenance-derived"


def test_statute_detected_status():
    info = get_provenance("STATUTE_DETECTED", None)
    assert "Statute" in info.label
    assert info.css_class == "provenance-statute-detected"


def test_statute_verified_status():
    info = get_provenance("STATUTE_VERIFIED", None)
    assert "Statute" in info.label
    assert "Verified" in info.label
    assert info.css_class == "provenance-statute-verified"


def test_error_status():
    info = get_provenance("ERROR", None)
    assert "Error" in info.label
    assert info.css_class == "provenance-error"


def test_unverified_no_token_status():
    info = get_provenance("UNVERIFIED_NO_TOKEN", None)
    assert "No API Token" in info.label or "Unverified" in info.label
    assert info.css_class == "provenance-unverified"


def test_unknown_status_returns_unknown():
    info = get_provenance("COMPLETELY_UNKNOWN_STATUS", None)
    assert info.label == "Unknown"
    assert info.css_class == "provenance-unknown"


def test_none_status_returns_unknown():
    info = get_provenance(None, None)
    assert info.css_class == "provenance-unknown"


# ── ProvenanceInfo dataclass ───────────────────────────────────────────────────


def test_provenance_info_is_frozen():
    info = ProvenanceInfo(label="Test", description="desc", css_class="cls")
    import pytest

    with pytest.raises(AttributeError):
        info.label = "other"  # type: ignore[misc]


def test_provenance_info_equality():
    a = ProvenanceInfo(label="X", description="d", css_class="c")
    b = ProvenanceInfo(label="X", description="d", css_class="c")
    assert a == b


# ── get_provenance_breakdown ───────────────────────────────────────────────────


@dataclass
class _FakeCitation:
    verification_status: str | None
    resolution_method: str | None
    normalized_text: str = ""
    raw_text: str = ""


def test_breakdown_empty_list():
    result = get_provenance_breakdown([])
    assert result == []


def test_breakdown_no_verified_citations():
    citations = [
        _FakeCitation("NOT_FOUND", None),
        _FakeCitation("AMBIGUOUS", None),
    ]
    result = get_provenance_breakdown(citations)
    assert result == []


def test_breakdown_single_direct_match():
    citations = [_FakeCitation("VERIFIED", "direct")]
    result = get_provenance_breakdown(citations)
    assert result == [("Direct Match", 1)]


def test_breakdown_multiple_methods():
    citations = [
        _FakeCitation("VERIFIED", "direct"),
        _FakeCitation("VERIFIED", "direct"),
        _FakeCitation("VERIFIED", "heuristic"),
        _FakeCitation("VERIFIED", "direct"),
        _FakeCitation("NOT_FOUND", None),
    ]
    result = get_provenance_breakdown(citations)
    labels = dict(result)
    assert labels["Direct Match"] == 3
    assert labels["Heuristic Match"] == 1


def test_breakdown_sorted_by_count_descending():
    citations = [
        _FakeCitation("VERIFIED", "heuristic"),
        _FakeCitation("VERIFIED", "direct"),
        _FakeCitation("VERIFIED", "direct"),
        _FakeCitation("VERIFIED", "direct"),
    ]
    result = get_provenance_breakdown(citations)
    assert result[0] == ("Direct Match", 3)
    assert result[1] == ("Heuristic Match", 1)


def test_breakdown_cache_with_resolution_cache_lookup():
    @dataclass
    class _C:
        verification_status: str
        resolution_method: str
        normalized_text: str
        raw_text: str = ""

    citations = [
        _C("VERIFIED", "cache", "123 U.S. 456"),
    ]
    resolution_cache: dict[str, Any] = {
        "123 U.S. 456": {"resolution_method": "heuristic"},
    }
    result = get_provenance_breakdown(citations, resolution_cache=resolution_cache)
    labels = dict(result)
    assert "Heuristic Match (cached)" in labels
    assert labels["Heuristic Match (cached)"] == 1


def test_breakdown_cache_without_cache_dict_uses_default():
    citations = [_FakeCitation("VERIFIED", "cache")]
    # No resolution_cache passed — falls back to "Direct Match (cached)"
    result = get_provenance_breakdown(citations, resolution_cache=None)
    assert result[0][0] == "Direct Match (cached)"


def test_breakdown_dedup_counts_as_direct_match():
    citations = [_FakeCitation("VERIFIED", "dedup")]
    result = get_provenance_breakdown(citations)
    assert result == [("Direct Match", 1)]


# ── PROVENANCE_HELP ────────────────────────────────────────────────────────────


def test_provenance_help_is_nonempty_list_of_tuples():
    assert isinstance(PROVENANCE_HELP, list)
    assert len(PROVENANCE_HELP) > 0
    for item in PROVENANCE_HELP:
        assert isinstance(item, tuple)
        assert len(item) == 2
        label, description = item
        assert isinstance(label, str) and label
        assert isinstance(description, str) and description


def test_provenance_help_contains_key_labels():
    labels = {label for label, _ in PROVENANCE_HELP}
    assert "Direct Match" in labels
    assert "Heuristic Match" in labels
    assert "Not Found" in labels
    assert "Statute — Verified" in labels


# ── Exporter integration ───────────────────────────────────────────────────────


def test_exporters_csv_includes_provenance_column():
    """CSV output must include a 'provenance' column."""
    from unittest.mock import MagicMock

    citation = MagicMock()
    citation.raw_text = "410 U.S. 113"
    citation.citation_type = "FullCaseCitation"
    citation.normalized_text = "410 U.S. 113"
    citation.resolved_from = "Roe v. Wade"
    citation.verification_status = "VERIFIED"
    citation.verification_detail = "found"
    citation.snippet = "the court held"
    citation.resolution_method = "direct"

    run = MagicMock()
    run.id = 1
    run.source_type = "text"
    run.source_name = None
    run.citation_count = 1
    run.verified_count = 1
    run.not_found_count = 0
    run.ambiguous_count = 0
    run.derived_count = 0
    run.statute_count = 0
    run.statute_verified_count = 0
    run.error_count = 0
    run.unverified_no_token_count = 0
    run.citations = [citation]

    from app.services.exporters import export_csv_for_run

    csv_output = export_csv_for_run(run)
    assert "provenance" in csv_output
    assert "Direct Match" in csv_output


def test_exporters_markdown_includes_provenance_line():
    """Markdown output must include a 'Provenance:' line per citation."""
    from unittest.mock import MagicMock

    citation = MagicMock()
    citation.raw_text = "410 U.S. 113"
    citation.citation_type = "FullCaseCitation"
    citation.normalized_text = "410 U.S. 113"
    citation.resolved_from = "Roe v. Wade"
    citation.verification_status = "NOT_FOUND"
    citation.verification_detail = "not found"
    citation.snippet = ""
    citation.resolution_method = None

    run = MagicMock()
    run.id = 2
    run.source_type = "text"
    run.source_name = None
    run.citation_count = 1
    run.verified_count = 0
    run.not_found_count = 1
    run.ambiguous_count = 0
    run.derived_count = 0
    run.statute_count = 0
    run.statute_verified_count = 0
    run.error_count = 0
    run.unverified_no_token_count = 0
    run.citations = [citation]

    from app.services.exporters import export_markdown_for_run

    md = export_markdown_for_run(run)
    assert "- Provenance: Not Found" in md
