"""Tests for Virginia Code statute detection in extract_citations.

Covers:
- Va. Code § X.Y-ZZZZ (standard)
- Va. Code Ann. § X.Y-ZZZZ (annotated)
- Code of Virginia § X.Y-ZZZZ
- Code of Va. § X.Y-ZZZZ
- Virginia Code § X.Y-ZZZZ
- Critical mixed text (1 case + 2 statutes)
- Statute-only text (no eyecite citations)
- No duplicates when same citation appears twice
- Position-based dedup: eyecite + regex same region
- Non-Virginia statutes still detected (M.R.S. still works)
- Citations flow through STATUTE_DETECTED → STATUTE_VERIFIED pipeline
"""

from __future__ import annotations

from app.services.audit import extract_citations
from app.services.verification import verify_citations

# ── Helpers ───────────────────────────────────────────────────────────────────


def _va_statute_citations(citations):
    """Return citations that look like Virginia Code statutes."""
    return [
        c
        for c in citations
        if c.citation_type == "FullLawCitation"
        and ("Va." in c.raw_text or "Virginia" in c.raw_text or "Code of Va" in c.raw_text)
    ]


# ── Critical mixed-text test ──────────────────────────────────────────────────


def test_critical_mixed_text_produces_three_citations():
    """The canonical bug-reproduction test: 1 case + 2 statutes from one sentence."""
    text = (
        "The process for boundary adjustment is governed by Va. Code § 15.2-3400"
        " and Va. Code § 15.2-1300."
        " See also Board of Supervisors v. City of Richmond, 539 U.S. 113 (2003)."
    )
    citations, warnings = extract_citations(text)
    assert len(citations) == 3, (
        f"Expected 3 citations, got {len(citations)}: {[c.raw_text for c in citations]}"
    )

    raw_texts = [c.raw_text for c in citations]
    assert any("15.2-3400" in r for r in raw_texts), "Missing Va. Code § 15.2-3400"
    assert any("15.2-1300" in r for r in raw_texts), "Missing Va. Code § 15.2-1300"
    assert any("539 U.S. 113" in r for r in raw_texts), "Missing 539 U.S. 113"


# ── Virginia Code format variants ─────────────────────────────────────────────


def test_va_code_standard_detected():
    text = "See Va. Code § 18.2-308 for the definition."
    citations, _ = extract_citations(text)
    va = _va_statute_citations(citations)
    assert va, "Expected Va. Code citation"
    assert "18.2-308" in va[0].raw_text


def test_va_code_ann_detected():
    text = "As defined in Va. Code Ann. § 46.2-100."
    citations, _ = extract_citations(text)
    va = _va_statute_citations(citations)
    assert va, "Expected Va. Code Ann. citation"
    assert "46.2-100" in va[0].raw_text


def test_code_of_virginia_detected():
    text = "This is governed by Code of Virginia § 15.2-3400."
    citations, _ = extract_citations(text)
    va = _va_statute_citations(citations)
    assert va, "Expected Code of Virginia citation"
    assert "15.2-3400" in va[0].raw_text


def test_code_of_va_detected():
    text = "Pursuant to Code of Va. § 18.2-308."
    citations, _ = extract_citations(text)
    va = _va_statute_citations(citations)
    assert va, "Expected Code of Va. citation"
    assert "18.2-308" in va[0].raw_text


def test_virginia_code_full_detected():
    text = "As provided in Virginia Code § 46.2-100."
    citations, _ = extract_citations(text)
    va = _va_statute_citations(citations)
    assert va, "Expected Virginia Code citation"
    assert "46.2-100" in va[0].raw_text


def test_code_of_virginia_with_year_detected():
    text = "Under Code of Virginia, 1950, as amended, § 15.2-1300."
    citations, _ = extract_citations(text)
    va = _va_statute_citations(citations)
    assert va, "Expected Code of Virginia (with year) citation"
    assert "15.2-1300" in va[0].raw_text


# ── "Section" and "Sec." as alternatives to § ────────────────────────────────


def test_va_code_section_word_detected():
    text = "This is governed by Va. Code Section 15.2-3400."
    citations, _ = extract_citations(text)
    va = _va_statute_citations(citations)
    assert va, "Expected Va. Code Section citation"
    assert "15.2-3400" in va[0].raw_text


def test_va_code_sec_dot_detected():
    text = "As provided in Va. Code Sec. 18.2-308."
    citations, _ = extract_citations(text)
    va = _va_statute_citations(citations)
    assert va, "Expected Va. Code Sec. citation"
    assert "18.2-308" in va[0].raw_text


def test_virginia_code_section_word_detected():
    text = "Pursuant to Virginia Code Section 46.2-100."
    citations, _ = extract_citations(text)
    va = _va_statute_citations(citations)
    assert va, "Expected Virginia Code Section citation"
    assert "46.2-100" in va[0].raw_text


def test_code_of_virginia_section_word_detected():
    text = "Under Code of Virginia Section 18.2-308."
    citations, _ = extract_citations(text)
    va = _va_statute_citations(citations)
    assert va, "Expected Code of Virginia Section citation"
    assert "18.2-308" in va[0].raw_text


def test_section_and_symbol_forms_both_detected():
    """'Va. Code Section X' and 'Va. Code § X' each produce their own citation."""
    text = "Va. Code Section 15.2-3400 (also written Va. Code § 15.2-3400)."
    citations, _ = extract_citations(text)
    hits = [c for c in citations if "15.2-3400" in c.raw_text]
    assert len(hits) == 2, (
        f"Expected 2 citations (one per form), got {len(hits)}: {[c.raw_text for c in hits]}"
    )


def test_section_word_type_is_full_law_citation():
    text = "Va. Code Section 18.2-308 is the relevant statute."
    citations, _ = extract_citations(text)
    va = [c for c in citations if "18.2-308" in c.raw_text]
    assert va, "Missing 18.2-308"
    assert va[0].citation_type == "FullLawCitation"


def test_section_word_has_snippet():
    text = "The defendant violated Va. Code Section 18.2-308 by carrying a weapon."
    citations, _ = extract_citations(text)
    va = [c for c in citations if "18.2-308" in c.raw_text]
    assert va, "Missing 18.2-308"
    assert va[0].snippet is not None
    assert "18.2-308" in va[0].snippet


def test_dotted_abbrev_section_word():
    """M.R.S. with 'Section' instead of § should also be detected."""
    text = "Pursuant to 20-A M.R.S. Section 1001, the board shall meet."
    citations, _ = extract_citations(text)
    mrs = [c for c in citations if "M.R.S." in c.raw_text and "1001" in c.raw_text]
    assert mrs, f"Expected M.R.S. Section 1001 citation; got {[c.raw_text for c in citations]}"


def test_dotted_abbrev_sec_dot():
    """M.R.S. with 'Sec.' instead of § should also be detected."""
    text = "Under 16 V.S.A. Sec. 833, the following applies."
    citations, _ = extract_citations(text)
    vsa = [c for c in citations if "V.S.A." in c.raw_text and "833" in c.raw_text]
    assert vsa, f"Expected V.S.A. Sec. 833 citation; got {[c.raw_text for c in citations]}"


def test_va_code_section_flows_into_verification():
    """'Va. Code Section X' form flows through to STATUTE_VERIFIED."""
    from app.services.verification import verify_citations

    text = "Va. Code Section 15.2-3400 governs voluntary settlements."
    citations, _ = extract_citations(text)

    va_verifier = _MockVAVerifier({"15.2-3400": ("STATUTE_VERIFIED", "Voluntary settlements")})
    result = verify_citations(
        citations,
        courtlistener_token=None,
        verification_base_url="https://www.courtlistener.com/api/rest/v4/citation-lookup/",
        virginia_statute_verifier=va_verifier,
    )
    va_cites = [c for c in result if "15.2-3400" in c.raw_text]
    assert va_cites, "No 15.2-3400 citation in result"
    assert va_cites[0].verification_status == "STATUTE_VERIFIED"
    assert "15.2-3400" in va_verifier.calls


def test_va_statute_type_is_full_law_citation():
    text = "Va. Code § 18.2-308 applies here."
    citations, _ = extract_citations(text)
    va = [c for c in citations if "18.2-308" in c.raw_text]
    assert va, "Missing 18.2-308"
    assert va[0].citation_type == "FullLawCitation"


def test_va_statute_has_snippet():
    text = "The defendant violated Va. Code § 18.2-308 by carrying a concealed weapon."
    citations, _ = extract_citations(text)
    va = [c for c in citations if "18.2-308" in c.raw_text]
    assert va, "Missing 18.2-308"
    assert va[0].snippet is not None
    assert "18.2-308" in va[0].snippet


# ── Statute-only text ─────────────────────────────────────────────────────────


def test_statute_only_text_finds_citation():
    """Text with only a Virginia statute (no case law) must still be detected."""
    text = "Va. Code § 15.2-3400 sets forth the procedure."
    citations, warnings = extract_citations(text)
    assert len(citations) == 1, f"Expected 1 citation, got {len(citations)}"
    assert "15.2-3400" in citations[0].raw_text
    assert citations[0].citation_type == "FullLawCitation"


def test_statute_only_text_no_no_citations_warning():
    text = "Va. Code § 15.2-3400 sets forth the procedure."
    _, warnings = extract_citations(text)
    assert not any("No citations" in w for w in warnings)


# ── Multiple Virginia statutes, no duplicates ─────────────────────────────────


def test_two_different_va_statutes_both_detected():
    text = "Va. Code § 15.2-3400 and Va. Code § 18.2-308 both apply."
    citations, _ = extract_citations(text)
    raw = [c.raw_text for c in citations]
    assert any("15.2-3400" in r for r in raw), "Missing 15.2-3400"
    assert any("18.2-308" in r for r in raw), "Missing 18.2-308"


def test_repeated_same_va_statute_not_duplicated():
    """The same Va. Code section appearing twice should produce one citation."""
    text = "Va. Code § 15.2-3400 applies. See also Va. Code § 15.2-3400 for details."
    citations, _ = extract_citations(text)
    hits = [c for c in citations if "15.2-3400" in c.raw_text]
    assert len(hits) == 1, f"Expected 1 citation for 15.2-3400, got {len(hits)}"


# ── Non-Virginia statutes still work ─────────────────────────────────────────


def test_maine_mrs_still_detected():
    """M.R.S. (Maine) detection must not be broken by the Virginia changes."""
    text = "Pursuant to 20-A M.R.S. § 1001(20), the board shall adopt policies."
    citations, _ = extract_citations(text)
    mrs = [c for c in citations if "M.R.S." in c.raw_text and "1001" in c.raw_text]
    assert mrs, f"M.R.S. § 1001 citation must be detected; got {[c.raw_text for c in citations]}"


def test_mixed_state_statutes_and_case_law():
    """Va. Code, M.R.S., and a case citation all in one paragraph."""
    text = (
        "Under Va. Code § 18.2-308 and 20-A M.R.S. § 1001(20), "
        "see Smith v. Jones, 123 F.3d 456 (1st Cir. 1999)."
    )
    citations, _ = extract_citations(text)
    raw = [c.raw_text for c in citations]
    assert any("18.2-308" in r for r in raw), "Missing Va. Code § 18.2-308"
    assert any("M.R.S." in r and "1001" in r for r in raw), "Missing M.R.S. § 1001"
    assert any("123 F.3d 456" in r for r in raw), "Missing 123 F.3d 456"


# ── Position-based dedup: eyecite and regex don't double-count ────────────────


def test_no_duplicate_when_eyecite_and_regex_overlap():
    """If eyecite extracts a statute text, the supplemental pass must not add it again."""
    # 42 U.S.C. § 1983 is typically extracted by eyecite as a FullLawCitation.
    # The dotted-abbrev pattern would also match it. Verify only one result.
    text = "Claims under 42 U.S.C. § 1983 are governed by federal law."
    citations, _ = extract_citations(text)
    hits = [c for c in citations if "1983" in c.raw_text]
    assert len(hits) == 1, f"Expected exactly 1 citation for § 1983, got {len(hits)}"


# ── Pipeline integration: STATUTE_DETECTED → STATUTE_VERIFIED ────────────────


class _MockVAVerifier:
    """Scriptable mock for VirginiaStatuteVerifier."""

    def __init__(self, responses: dict) -> None:
        self._responses = responses
        self.calls: list[str] = []

    def verify(self, section_number: str) -> tuple[str, str | None]:
        self.calls.append(section_number)
        return self._responses.get(section_number, ("STATUTE_ERROR", None))


def test_detected_va_statute_flows_into_verification():
    """Statutes found by extract_citations flow through to STATUTE_VERIFIED."""
    text = "Va. Code § 15.2-3400 governs voluntary settlements."
    citations, _ = extract_citations(text)

    va_verifier = _MockVAVerifier({"15.2-3400": ("STATUTE_VERIFIED", "Voluntary settlements")})
    result = verify_citations(
        citations,
        courtlistener_token=None,
        verification_base_url="https://www.courtlistener.com/api/rest/v4/citation-lookup/",
        virginia_statute_verifier=va_verifier,
    )
    va_cites = [c for c in result if "15.2-3400" in c.raw_text]
    assert va_cites, "No 15.2-3400 citation in result"
    assert va_cites[0].verification_status == "STATUTE_VERIFIED"
    assert "15.2-3400" in va_verifier.calls


def test_critical_text_end_to_end_statuses():
    """Full pipeline: critical text produces correct verification statuses."""
    text = (
        "The process for boundary adjustment is governed by Va. Code § 15.2-3400"
        " and Va. Code § 15.2-1300."
        " See also Board of Supervisors v. City of Richmond, 539 U.S. 113 (2003)."
    )
    citations, _ = extract_citations(text)
    va_verifier = _MockVAVerifier(
        {
            "15.2-3400": ("STATUTE_VERIFIED", "Voluntary settlements"),
            "15.2-1300": ("STATUTE_VERIFIED", "Settlement authority"),
        }
    )
    result = verify_citations(
        citations,
        courtlistener_token=None,
        verification_base_url="https://www.courtlistener.com/api/rest/v4/citation-lookup/",
        virginia_statute_verifier=va_verifier,
    )
    statuses = {c.raw_text: c.verification_status for c in result}
    assert any("15.2-3400" in k for k in statuses), "15.2-3400 not in result"
    assert any("15.2-1300" in k for k in statuses), "15.2-1300 not in result"
    for key, status in statuses.items():
        if "15.2-3400" in key or "15.2-1300" in key:
            assert status == "STATUTE_VERIFIED", f"{key} should be STATUTE_VERIFIED, got {status}"
        elif "539 U.S. 113" in key:
            assert status == "UNVERIFIED_NO_TOKEN", (
                "Case law without token should be UNVERIFIED_NO_TOKEN"
            )


# ── Frankenstein document patterns (exact text) ────────────────────────────


def test_frankenstein_va_code_no_ann_detected():
    """Va. Code § 15.2-3400 — no Ann. suffix."""
    text = "Va. Code § 15.2-3400 governs boundary adjustments."
    citations, _ = extract_citations(text)
    hits = [c for c in citations if "15.2-3400" in c.raw_text]
    assert hits, f"Expected Va. Code § 15.2-3400; got {[c.raw_text for c in citations]}"


def test_frankenstein_va_code_1301_detected():
    """Va. Code § 15.2-1301."""
    text = "The authority is Va. Code § 15.2-1301."
    citations, _ = extract_citations(text)
    hits = [c for c in citations if "15.2-1301" in c.raw_text]
    assert hits, f"Expected Va. Code § 15.2-1301; got {[c.raw_text for c in citations]}"


def test_frankenstein_virginia_code_section_word_detected():
    """Virginia Code Section 8.01-654 — full name + 'Section' keyword."""
    text = "See Virginia Code Section 8.01-654 for habeas procedure."
    citations, _ = extract_citations(text)
    hits = [c for c in citations if "8.01-654" in c.raw_text]
    assert hits, f"Expected 8.01-654; got {[c.raw_text for c in citations]}"


def test_frankenstein_va_code_sec_dot_detected():
    """Va. Code Sec. 19.2-327.2 — 'Sec.' abbreviation."""
    text = "As provided by Va. Code Sec. 19.2-327.2."
    citations, _ = extract_citations(text)
    hits = [c for c in citations if "19.2-327.2" in c.raw_text]
    assert hits, f"Expected 19.2-327.2; got {[c.raw_text for c in citations]}"


def test_frankenstein_virginia_code_et_seq_detected():
    """Virginia Code § 1-200 et seq. — 'et seq.' suffix."""
    text = "Pursuant to Virginia Code § 1-200 et seq., the following rules apply."
    citations, _ = extract_citations(text)
    hits = [c for c in citations if "1-200" in c.raw_text]
    assert hits, f"Expected 1-200; got {[c.raw_text for c in citations]}"


def test_frankenstein_all_six_patterns():
    """All six Frankenstein Virginia citations detected in one document."""
    text = (
        "Va. Code § 15.2-3400 governs boundary adjustments. "
        "Va. Code Ann. § 15.2-1300 provides authority. "
        "Va. Code § 15.2-1301 is also relevant. "
        "Virginia Code Section 8.01-654 covers habeas corpus. "
        "Va. Code Sec. 19.2-327.2 addresses the writ. "
        "Virginia Code § 1-200 et seq. provides general definitions."
    )
    citations, _ = extract_citations(text)
    raw_texts = [c.raw_text for c in citations]
    assert any("15.2-3400" in r for r in raw_texts), "Missing Va. Code § 15.2-3400"
    assert any("15.2-1300" in r for r in raw_texts), "Missing Va. Code Ann. § 15.2-1300"
    assert any("15.2-1301" in r for r in raw_texts), "Missing Va. Code § 15.2-1301"
    assert any("8.01-654" in r for r in raw_texts), "Missing Virginia Code Section 8.01-654"
    assert any("19.2-327.2" in r for r in raw_texts), "Missing Va. Code Sec. 19.2-327.2"
    assert any("1-200" in r for r in raw_texts), "Missing Virginia Code § 1-200 et seq."


# ── Other state statute patterns (ISSUE 4) ────────────────────────────────


def test_texas_educ_code_detected():
    """Tex. Educ. Code § 26.010 — Texas Education Code."""
    text = "Under Tex. Educ. Code § 26.010, parents have rights."
    citations, _ = extract_citations(text)
    hits = [c for c in citations if "26.010" in c.raw_text]
    assert hits, f"Expected Tex. Educ. Code § 26.010; got {[c.raw_text for c in citations]}"


def test_ohio_rev_code_detected():
    """Ohio Rev. Code § 4112.02 — Ohio Revised Code."""
    text = "Ohio Rev. Code § 4112.02 prohibits discrimination."
    citations, _ = extract_citations(text)
    hits = [c for c in citations if "4112.02" in c.raw_text]
    assert hits, f"Expected Ohio Rev. Code § 4112.02; got {[c.raw_text for c in citations]}"


def test_maine_mrs_section_6552_detected():
    """20-A M.R.S. § 6552 — Maine Revised Statutes."""
    text = "Pursuant to 20-A M.R.S. § 6552, the board shall act."
    citations, _ = extract_citations(text)
    hits = [c for c in citations if "6552" in c.raw_text]
    assert hits, f"Expected 20-A M.R.S. § 6552; got {[c.raw_text for c in citations]}"
