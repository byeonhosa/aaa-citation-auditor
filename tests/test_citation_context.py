"""Tests for Bluebook-format citation context extraction.

The _extract_bluebook_citation() helper (used by citation_to_context) should
produce "Party v. Party, vol. reporter page (court year)" strings from
document snippets, handling:
  - Standard "Party v. Party" case names
  - Court parentheticals: (4th Cir. 2001), (Va. 2012), etc.
  - Parallel reporter citations: "265 Va. 505, 578 S.E.2d 781 (2003)"
  - Special name prefixes: "In re ...", "Ex parte ..."
  - Graceful fallbacks for short citations and missing case names
"""

from __future__ import annotations

from app.routes.pages import _extract_bluebook_citation

# ── Standard "Party v. Party" cases ──────────────────────────────────────────


def test_scotus_citation_extracts_name_and_year() -> None:
    snippet = "See Brown v. Board of Education, 347 U.S. 483 (1954). The ruling changed everything."
    assert _extract_bluebook_citation(snippet, "347 U.S. 483") == (
        "Brown v. Board of Education, 347 U.S. 483 (1954)"
    )


def test_circuit_court_citation_includes_court_parenthetical() -> None:
    snippet = "United States v. Robinson, 275 F.3d 371 (4th Cir. 2001)"
    assert _extract_bluebook_citation(snippet, "275 F.3d 371") == (
        "United States v. Robinson, 275 F.3d 371 (4th Cir. 2001)"
    )


def test_multi_word_defendant_extracts_correctly() -> None:
    snippet = "Billups v. City of Charleston, 961 F.3d 673 (4th Cir. 2020)"
    assert _extract_bluebook_citation(snippet, "961 F.3d 673") == (
        "Billups v. City of Charleston, 961 F.3d 673 (4th Cir. 2020)"
    )


def test_multi_word_plaintiff_extracts_correctly() -> None:
    snippet = "United States v. Robinson, 275 F.3d 371 (4th Cir. 2001)"
    assert _extract_bluebook_citation(snippet, "275 F.3d 371") == (
        "United States v. Robinson, 275 F.3d 371 (4th Cir. 2001)"
    )


# ── Parallel citations ────────────────────────────────────────────────────────


def test_parallel_citation_captured() -> None:
    snippet = "Commonwealth v. Hudson, 265 Va. 505, 578 S.E.2d 781 (2003)."
    assert _extract_bluebook_citation(snippet, "265 Va. 505") == (
        "Commonwealth v. Hudson, 265 Va. 505, 578 S.E.2d 781 (2003)"
    )


# ── Special case-name prefixes ────────────────────────────────────────────────


def test_in_re_prefix_handled() -> None:
    snippet = "court held in In re Grand Jury Subpoena, 223 F.3d 213 (3d Cir. 2000) that"
    assert _extract_bluebook_citation(snippet, "223 F.3d 213") == (
        "In re Grand Jury Subpoena, 223 F.3d 213 (3d Cir. 2000)"
    )


def test_ex_parte_prefix_handled() -> None:
    snippet = "Ex parte Young, 209 U.S. 123 (1908) established the doctrine."
    assert _extract_bluebook_citation(snippet, "209 U.S. 123") == (
        "Ex parte Young, 209 U.S. 123 (1908)"
    )


# ── Multiple citations in snippet — pick the right one ───────────────────────


def test_selects_correct_case_when_multiple_citations_in_snippet() -> None:
    """Anderson v. Creighton, not Harlow v. Fitzgerald, for the 483 U.S. 635 cite."""
    snippet = (
        "Harlow v. Fitzgerald, 457 U.S. 800 (1982). "
        "Anderson v. Creighton, 483 U.S. 635 (1987) refined the test."
    )
    assert _extract_bluebook_citation(snippet, "483 U.S. 635") == (
        "Anderson v. Creighton, 483 U.S. 635 (1987)"
    )


def test_preceding_sentence_words_not_included_in_plaintiff() -> None:
    """'The foundational case is Brown v. Board' → plaintiff is Brown, not 'The foundational...'"""
    snippet = (
        "The foundational case on this issue is Brown v. Board of Education, 347 U.S. 483 (1954)"
    )
    result = _extract_bluebook_citation(snippet, "347 U.S. 483")
    assert result == "Brown v. Board of Education, 347 U.S. 483 (1954)"


def test_see_signal_not_included_in_plaintiff() -> None:
    snippet = "See Anderson v. Creighton, 483 U.S. 635 (1987)."
    result = _extract_bluebook_citation(snippet, "483 U.S. 635")
    assert result == "Anderson v. Creighton, 483 U.S. 635 (1987)"


# ── Fallback cases ────────────────────────────────────────────────────────────


def test_short_citation_returns_none() -> None:
    """Short citations like '275 F.3d at 378' have no case name — return None."""
    snippet = "See 275 F.3d at 378 for details."
    assert _extract_bluebook_citation(snippet, "275 F.3d at 378") is None


def test_raw_text_not_in_snippet_returns_none() -> None:
    assert _extract_bluebook_citation("Some other text entirely.", "347 U.S. 483") is None


def test_empty_inputs_return_none() -> None:
    assert _extract_bluebook_citation("", "347 U.S. 483") is None
    assert _extract_bluebook_citation("Brown v. Board, 347 U.S. 483 (1954)", "") is None


def test_no_garbage_text_in_output() -> None:
    """Verify that output never includes full preceding sentences as the case name."""
    snippet = (
        "The court noted that the standard for qualified immunity was first articulated "
        "in Harlow v. Fitzgerald, 457 U.S. 800 (1982)."
    )
    result = _extract_bluebook_citation(snippet, "457 U.S. 800")
    assert result is not None
    assert "qualified immunity" not in result
    assert "The court" not in result
    assert result == "Harlow v. Fitzgerald, 457 U.S. 800 (1982)"
