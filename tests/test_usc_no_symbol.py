"""Tests for U.S.C. citations without § symbol."""

from __future__ import annotations

from app.services.audit import _USC_NO_SYMBOL_RE, extract_citations
from app.services.statute_verification import parse_federal_section


class TestUscNoSymbolRegex:
    def test_basic_no_symbol(self):
        assert _USC_NO_SYMBOL_RE.search("35 U.S.C. 154") is not None

    def test_subsection_a_1(self):
        assert _USC_NO_SYMBOL_RE.search("35 U.S.C. 154(a)(1)") is not None

    def test_subsection_a(self):
        assert _USC_NO_SYMBOL_RE.search("28 U.S.C. 1257(a)") is not None

    def test_et_seq(self):
        assert _USC_NO_SYMBOL_RE.search("35 U.S.C. 1 et seq.") is not None

    def test_standard_with_symbol_not_matched(self):
        # "42 U.S.C. § 1983" should NOT be caught by this pattern (has §)
        assert _USC_NO_SYMBOL_RE.search("42 U.S.C. § 1983") is None

    def test_section_keyword_not_matched(self):
        # "42 U.S.C. Section 1983" should NOT be caught (has Section keyword)
        assert _USC_NO_SYMBOL_RE.search("42 U.S.C. Section 1983") is None


class TestParseNoSymbolCitations:
    def test_bare_number_parsed(self):
        result = parse_federal_section("35 U.S.C. 154")
        assert result == ("35", "154")

    def test_subsection_stripped(self):
        result = parse_federal_section("35 U.S.C. 154(a)(1)")
        assert result == ("35", "154")

    def test_subsection_a_stripped(self):
        result = parse_federal_section("28 U.S.C. 1257(a)")
        assert result == ("28", "1257")

    def test_et_seq_parsed(self):
        result = parse_federal_section("35 U.S.C. 1 et seq.")
        assert result == ("35", "1")

    def test_standard_with_symbol_still_works(self):
        result = parse_federal_section("42 U.S.C. § 1983")
        assert result == ("42", "1983")

    def test_usc_no_periods(self):
        result = parse_federal_section("42 USC § 1983")
        assert result == ("42", "1983")


class TestExtractCitationsUscNoSymbol:
    def test_detected_as_statute(self):
        text = "Under 35 U.S.C. 154(a)(1), a patent term is twenty years."
        citations, _ = extract_citations(text)
        raw_texts = [c.raw_text for c in citations]
        assert any("35 U.S.C." in t for t in raw_texts), f"No U.S.C. citation in {raw_texts}"

    def test_detected_in_context(self):
        text = "Plaintiff asserts claims under 28 U.S.C. 1257(a) for review."
        citations, _ = extract_citations(text)
        raw_texts = [c.raw_text for c in citations]
        assert any("28 U.S.C." in t for t in raw_texts), f"No U.S.C. citation in {raw_texts}"

    def test_et_seq_detected(self):
        text = "Regulated under 15 U.S.C. 78j et seq., the Act applies."
        citations, _ = extract_citations(text)
        raw_texts = [c.raw_text for c in citations]
        assert any("15 U.S.C." in t for t in raw_texts), f"No U.S.C. citation in {raw_texts}"
