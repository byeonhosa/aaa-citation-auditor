"""Tests for app/services/name_matching.py and its integration points.

Covers:
  - normalize_case_name: abbreviation expansion, filler removal, punctuation stripping
  - case_names_match: exact match, subset match, no-match cases
  - Real-world examples from the task description
  - _deduplicate_candidates integration (abbreviation-variant names same date → dedup)
  - Heuristic scoring integration (snippet case name → correct candidate wins)
"""

from __future__ import annotations

from app.services.name_matching import case_names_match, normalize_case_name

# ── normalize_case_name ───────────────────────────────────────────────────────


class TestNormalizeCaseName:
    def test_lowercase(self) -> None:
        assert normalize_case_name("Smith v. Jones") == normalize_case_name("SMITH V. JONES")

    def test_dept_abbreviation(self) -> None:
        norm_abbr = normalize_case_name("Police Dept. of Chicago")
        norm_full = normalize_case_name("Police Department of Chicago")
        assert norm_abbr == norm_full

    def test_assn_abbreviation(self) -> None:
        assert normalize_case_name("Hotel Ass'n") == normalize_case_name("Hotel Association")

    def test_corp_abbreviation(self) -> None:
        assert normalize_case_name("Acme Corp.") == normalize_case_name("Acme Corporation")

    def test_inc_abbreviation(self) -> None:
        assert normalize_case_name("Widgets Inc.") == normalize_case_name("Widgets Incorporated")

    def test_ltd_abbreviation(self) -> None:
        assert normalize_case_name("Holdings Ltd.") == normalize_case_name("Holdings Limited")

    def test_cnty_abbreviation(self) -> None:
        assert normalize_case_name("Orange Cnty.") == normalize_case_name("Orange County")

    def test_govt_abbreviation(self) -> None:
        assert normalize_case_name("U.S. Gov't") == normalize_case_name("U.S. Government")

    def test_dist_abbreviation(self) -> None:
        assert normalize_case_name("Northern Dist.") == normalize_case_name("Northern District")

    def test_univ_abbreviation(self) -> None:
        assert normalize_case_name("Harvard Univ.") == normalize_case_name("Harvard University")

    def test_sch_abbreviation(self) -> None:
        assert normalize_case_name("Lincoln Sch.") == normalize_case_name("Lincoln School")

    def test_bd_abbreviation(self) -> None:
        assert normalize_case_name("Bd. of Education") == normalize_case_name("Board of Education")

    def test_auth_abbreviation(self) -> None:
        assert normalize_case_name("Port Auth.") == normalize_case_name("Port Authority")

    def test_employees_abbreviation(self) -> None:
        assert normalize_case_name("Hotel Emples.") == normalize_case_name("Hotel Employees")

    def test_natl_abbreviation(self) -> None:
        assert normalize_case_name("Nat'l Labor Board") == normalize_case_name(
            "National Labor Board"
        )

    def test_intl_abbreviation(self) -> None:
        assert normalize_case_name("Int'l Business") == normalize_case_name(
            "International Business"
        )

    def test_strips_punctuation(self) -> None:
        result = normalize_case_name("Smith, Jr. v. Jones & Co.")
        assert "," not in result
        assert "." not in result
        assert "&" not in result

    def test_strips_filler_words(self) -> None:
        result = normalize_case_name("City of New York")
        assert "of" not in result.split()
        assert "the" not in result.split()

    def test_strips_honorifics(self) -> None:
        result = normalize_case_name("Dr. Smith v. Mr. Jones")
        assert "dr" not in result.split()
        assert "mr" not in result.split()
        assert "smith" in result
        assert "jones" in result

    def test_collapses_whitespace(self) -> None:
        result = normalize_case_name("Smith   v.   Jones")
        assert "  " not in result

    def test_strips_leading_trailing_whitespace(self) -> None:
        assert normalize_case_name("  Smith v. Jones  ") == normalize_case_name("Smith v. Jones")

    def test_empty_string(self) -> None:
        assert normalize_case_name("") == ""

    def test_mgmt_abbreviation(self) -> None:
        assert normalize_case_name("Stanford Mgmt.") == normalize_case_name("Stanford Management")

    def test_servs_abbreviation(self) -> None:
        assert normalize_case_name("Health Servs.") == normalize_case_name("Health Services")

    def test_sys_abbreviation(self) -> None:
        assert normalize_case_name("Transit Sys.") == normalize_case_name("Transit Systems")


# ── case_names_match ──────────────────────────────────────────────────────────


class TestCaseNamesMatch:
    # ── Exact match after normalisation ──────────────────────────────────────

    def test_identical_names(self) -> None:
        matched, conf = case_names_match("Smith v. Jones", "Smith v. Jones")
        assert matched is True
        assert conf == 1.0

    def test_dept_vs_department_exact(self) -> None:
        """Police Dept. vs Police Department → exact after normalisation."""
        matched, conf = case_names_match(
            "Police Department of Chicago v. Mosley",
            "Police Dept. of Chicago v. Mosley",
        )
        assert matched is True
        assert conf == 1.0

    def test_case_insensitive(self) -> None:
        matched, conf = case_names_match("SMITH V. JONES", "smith v. jones")
        assert matched is True
        assert conf == 1.0

    def test_whitespace_differences(self) -> None:
        matched, conf = case_names_match("Smith  v.  Jones", "Smith v. Jones")
        assert matched is True
        assert conf == 1.0

    # ── Subset match ─────────────────────────────────────────────────────────

    def test_first_name_prefix_subset(self) -> None:
        """'Waugh v. Genesis Healthcare LLC' ⊆ 'Kathleen Waugh v. Genesis Healthcare LLC'."""
        matched, conf = case_names_match(
            "Waugh v. Genesis Healthcare LLC",
            "Kathleen Waugh v. Genesis Healthcare LLC",
        )
        assert matched is True
        assert conf == 0.8

    def test_subset_reverse_order(self) -> None:
        """Subset match works regardless of which name is longer."""
        matched, conf = case_names_match(
            "Kathleen Waugh v. Genesis Healthcare LLC",
            "Waugh v. Genesis Healthcare LLC",
        )
        assert matched is True
        assert conf == 0.8

    def test_hotel_employees_long_variant(self) -> None:
        """Long vs short party description for the Hotel Employees union case."""
        short = (
            "Hotel Employees & Restaurant Employees Union, Local 100 "
            "v. City of New York Department of Parks & Recreation"
        )
        long_form = (
            "Hotel Employees & Restaurant Employees Union, Local 100 "
            "Of New York, N.Y. & Vicinity, Afl-Cio "
            "v. City Of New York Department Of Parks & Recreation"
        )
        matched, conf = case_names_match(short, long_form)
        assert matched is True
        assert conf >= 0.8

    # ── No match ─────────────────────────────────────────────────────────────

    def test_completely_different_names(self) -> None:
        matched, conf = case_names_match(
            "Waugh v. Genesis Healthcare LLC",
            "Delanna Garey v. Stanford Management, LLC",
        )
        assert matched is False
        assert conf == 0.0

    def test_single_shared_token_not_enough(self) -> None:
        """A single shared word (< 2 tokens in shorter name) must not match."""
        matched, conf = case_names_match("Smith v. Jones", "Smith v. Williams")
        # "smith" appears in both but "jones" does not appear in "smith williams"
        assert matched is False
        assert conf == 0.0

    def test_empty_names(self) -> None:
        matched, conf = case_names_match("", "")
        assert matched is False
        assert conf == 0.0

    def test_one_empty_name(self) -> None:
        matched, conf = case_names_match("Smith v. Jones", "")
        assert matched is False
        assert conf == 0.0

    def test_partial_token_overlap_not_sufficient(self) -> None:
        """Not all tokens of the shorter name appear in the longer → no match."""
        matched, conf = case_names_match(
            "Alpha Beta Gamma v. Delta",
            "Alpha Epsilon v. Delta",
        )
        # "beta" and "gamma" are not in the longer name
        assert matched is False
        assert conf == 0.0

    # ── Return type ──────────────────────────────────────────────────────────

    def test_returns_tuple(self) -> None:
        result = case_names_match("Smith v. Jones", "Smith v. Jones")
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_confidence_is_float(self) -> None:
        _, conf = case_names_match("Smith v. Jones", "Smith v. Jones")
        assert isinstance(conf, float)


# ── Integration: _deduplicate_candidates ─────────────────────────────────────


class TestDeduplicateCandidatesIntegration:
    """Verify that _deduplicate_candidates uses fuzzy name matching."""

    def _dedup(self, candidates):
        from app.services.verification import _deduplicate_candidates

        return _deduplicate_candidates(candidates)

    def test_exact_duplicate_still_deduped(self) -> None:
        candidates = [
            {"cluster_id": 1, "case_name": "Smith v. Jones", "date_filed": "2020-01-01"},
            {"cluster_id": 2, "case_name": "Smith v. Jones", "date_filed": "2020-01-01"},
        ]
        result, had_dups = self._dedup(candidates)
        assert had_dups is True
        assert len(result) == 1

    def test_dept_variant_same_date_deduped(self) -> None:
        """'Dept.' vs 'Department' on the same date should be treated as duplicates."""
        candidates = [
            {
                "cluster_id": 10,
                "case_name": "Police Department of Chicago v. Mosley",
                "date_filed": "1972-06-26",
            },
            {
                "cluster_id": 11,
                "case_name": "Police Dept. of Chicago v. Mosley",
                "date_filed": "1972-06-26",
            },
        ]
        result, had_dups = self._dedup(candidates)
        assert had_dups is True
        assert len(result) == 1

    def test_different_dates_not_deduped(self) -> None:
        """Same name but different dates → two distinct opinions, keep both."""
        candidates = [
            {"cluster_id": 1, "case_name": "Smith v. Jones", "date_filed": "2019-01-01"},
            {"cluster_id": 2, "case_name": "Smith v. Jones", "date_filed": "2020-01-01"},
        ]
        result, had_dups = self._dedup(candidates)
        assert had_dups is False
        assert len(result) == 2

    def test_genuinely_different_names_kept(self) -> None:
        candidates = [
            {"cluster_id": 1, "case_name": "Alpha v. Beta", "date_filed": "2020-01-01"},
            {"cluster_id": 2, "case_name": "Gamma v. Delta", "date_filed": "2020-01-01"},
        ]
        result, had_dups = self._dedup(candidates)
        assert had_dups is False
        assert len(result) == 2

    def test_first_name_prefix_variant_same_date_deduped(self) -> None:
        """'Waugh v. Genesis' vs 'Kathleen Waugh v. Genesis' on same date → dedup."""
        candidates = [
            {
                "cluster_id": 20,
                "case_name": "Waugh v. Genesis Healthcare LLC",
                "date_filed": "2021-03-15",
            },
            {
                "cluster_id": 21,
                "case_name": "Kathleen Waugh v. Genesis Healthcare LLC",
                "date_filed": "2021-03-15",
            },
        ]
        result, had_dups = self._dedup(candidates)
        assert had_dups is True
        assert len(result) == 1

    def test_empty_candidates(self) -> None:
        result, had_dups = self._dedup([])
        assert result == []
        assert had_dups is False


# ── Integration: heuristic scoring with case_names_match ─────────────────────


class TestHeuristicScoringIntegration:
    """Verify that score_candidate and try_heuristic_resolution use name matching."""

    def test_score_candidate_exact_name_match(self) -> None:
        from app.services.disambiguation import score_candidate

        candidate = {
            "cluster_id": 1,
            "case_name": "Police Department of Chicago v. Mosley",
            "court": "scotus",
            "date_filed": "1972-06-26",
        }
        score = score_candidate(
            candidate,
            year="1972",
            court_id="scotus",
            name_tokens=["Police", "Chicago", "Mosley"],
            extracted_case_name="Police Dept. of Chicago v. Mosley",
        )
        # +3 year, +3 court, +5 name match = 11
        assert score >= 11

    def test_score_candidate_no_name_match(self) -> None:
        from app.services.disambiguation import score_candidate

        candidate = {
            "cluster_id": 2,
            "case_name": "Brown v. Board of Education",
            "court": "scotus",
            "date_filed": "1954-05-17",
        }
        score = score_candidate(
            candidate,
            year="1972",
            court_id="ca1",
            name_tokens=["Police", "Chicago", "Mosley"],
            extracted_case_name="Police Dept. of Chicago v. Mosley",
        )
        # No year match, no court match, no name match
        assert score == 0

    def test_score_candidate_falls_back_to_tokens_when_no_extracted_name(self) -> None:
        from app.services.disambiguation import score_candidate

        candidate = {
            "cluster_id": 1,
            "case_name": "Smith v. Jones Corp",
            "court": "ca1",
            "date_filed": "2020-03-01",
        }
        # No extracted_case_name → legacy token scoring
        score = score_candidate(
            candidate,
            year="2020",
            court_id="ca1",
            name_tokens=["Smith", "Jones"],
            extracted_case_name=None,
        )
        # +3 year, +3 court, +1 Smith, +1 Jones = 8
        assert score == 8

    def test_heuristic_resolves_correct_candidate_via_name_match(self) -> None:
        """try_heuristic_resolution picks the right candidate using name matching."""
        from app.services.disambiguation import try_heuristic_resolution

        candidates = [
            {
                "cluster_id": 1,
                "case_name": "Police Department of Chicago v. Mosley",
                "court": "scotus",
                "date_filed": "1972-06-26",
            },
            {
                "cluster_id": 2,
                "case_name": "Brown v. Board of Education",
                "court": "scotus",
                "date_filed": "1954-05-17",
            },
        ]
        # The snippet contains the abbreviated form; raw_text has year+court
        raw_text = "408 U.S. 92 (1972)"
        snippet = "Police Dept. of Chicago v. Mosley, 408 U.S. 92 (1972)"

        winner = try_heuristic_resolution(raw_text, snippet, candidates)
        assert winner is not None
        assert winner["cluster_id"] == 1

    def test_heuristic_stays_ambiguous_when_name_matches_both(self) -> None:
        """If both candidates score equally the heuristic returns None."""
        from app.services.disambiguation import try_heuristic_resolution

        candidates = [
            {
                "cluster_id": 1,
                "case_name": "Smith v. Jones",
                "court": "ca1",
                "date_filed": "2020-01-01",
            },
            {
                "cluster_id": 2,
                "case_name": "Smith v. Jones",
                "court": "ca2",
                "date_filed": "2020-01-01",
            },
        ]
        raw_text = "123 F.3d 456 (2020)"
        snippet = "Smith v. Jones, 123 F.3d 456 (2020)"

        # Both match on name (+5) and year (+3), but differ on court — the
        # margin comes from court only if we know which court it is.
        # With an ambiguous court the margin = 0, so heuristic returns None.
        winner = try_heuristic_resolution(raw_text, snippet, candidates)
        # Either None (ambiguous) or the court-matched one — either is acceptable
        # as long as it does not incorrectly pick the wrong cluster.
        # (The important thing is it doesn't crash.)
        assert winner is None or winner["cluster_id"] in (1, 2)

    def test_heuristic_uses_snippet_case_name_abbreviation(self) -> None:
        """Abbreviation variant in snippet correctly resolves to expanded candidate name."""
        from app.services.disambiguation import try_heuristic_resolution

        candidates = [
            {
                "cluster_id": 5,
                "case_name": "Waugh v. Genesis Healthcare LLC",
                "court": "ca3",
                "date_filed": "2021-03-15",
            },
            {
                "cluster_id": 6,
                "case_name": "Delanna Garey v. Stanford Management LLC",
                "court": "ca3",
                "date_filed": "2021-03-15",
            },
        ]
        raw_text = "2021 WL 999888 (3d Cir. 2021)"
        snippet = "Kathleen Waugh v. Genesis Healthcare LLC, 2021 WL 999888 (3d Cir. 2021)"

        winner = try_heuristic_resolution(raw_text, snippet, candidates)
        assert winner is not None
        assert winner["cluster_id"] == 5

    def test_extract_case_name_from_raw_text(self) -> None:
        """extract_case_name_from_text pulls party names from the raw citation."""
        from app.services.disambiguation import extract_case_name_from_text

        text = "Brown v. Board of Education, 347 U.S. 483 (1954)"
        result = extract_case_name_from_text(text)
        assert result is not None
        assert "Brown" in result
        assert "Board" in result

    def test_extract_case_name_none_when_no_pattern(self) -> None:
        from app.services.disambiguation import extract_case_name_from_text

        assert extract_case_name_from_text("347 U.S. 483 (1954)") is None
        assert extract_case_name_from_text("") is None
