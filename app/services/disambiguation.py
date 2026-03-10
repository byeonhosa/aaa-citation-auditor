"""Heuristic auto-disambiguation for AMBIGUOUS CourtListener results.

When CourtListener returns multiple candidate clusters for a citation, this
module tries to pick the correct one using context clues extracted from the
citation text and surrounding snippet.

Scoring rules
-------------
+3   Year in parenthetical matches candidate's date_filed year
+3   Court in parenthetical matches candidate's court_id
+5   Case name extracted from raw_text/snippet matches candidate's case_name
     with confidence >= 0.8 (via case_names_match — handles abbreviations)
+1   Each raw case-name token found in candidate's case_name (fallback when
     no high-confidence name match; legacy behaviour)

Selection rules
---------------
The best candidate wins only when:
  - Its score is at least MIN_SCORE (some context must match)
  - It leads the next-best by at least MIN_MARGIN (must be unambiguous)
Otherwise the function returns None and the citation stays AMBIGUOUS for
the user to resolve manually.
"""

from __future__ import annotations

import logging
import re

from app.services.name_matching import case_names_match

logger = logging.getLogger(__name__)

# ── Scoring thresholds ──────────────────────────────────────────────────────

MIN_SCORE = 3
MIN_MARGIN = 2

# ── Compiled patterns ───────────────────────────────────────────────────────

# Extract a "Party v. Party" case name from free text (snippet or raw_text).
# Captures up to ~60 chars per party; stops at reporter digits, commas or parens.
_SNIPPET_CASE_NAME_RE = re.compile(
    r"([A-Z][A-Za-z''\-&.,\s]{1,60}?)\s+v\.\s+([A-Z][A-Za-z''\-&.,\s]{1,60}?)(?=\s*[,(]|\s*\d|\Z)",
)

# Match the ordinal-circuit pattern inside a parenthetical:
# "(6th Cir.", "(2d Cir.", "(11th Cir.", etc.
_CIRCUIT_RE = re.compile(
    r"\((?:[^)]*?\s+)?(\d+)(?:st|nd|rd|th|d)\s+Cir\.",
    re.IGNORECASE,
)
_DC_CIR_RE = re.compile(r"\((?:[^)]*?\s+)?D\.C\.\s+Cir\.", re.IGNORECASE)
_FED_CIR_RE = re.compile(r"\((?:[^)]*?\s+)?Fed\.\s+Cir\.", re.IGNORECASE)

# SCOTUS: reporter pattern volume + U.S./S.Ct./L.Ed. + page
_SCOTUS_REPORTER_RE = re.compile(
    r"\d+\s+U\.S\.\s+\d+"
    r"|\d+\s+S\.\s*Ct\.\s+\d+"
    r"|\d+\s+L\.\s*Ed\.(?:\s+2d)?\s+\d+"
)

# Extract the last 4-digit year from a parenthetical
_YEAR_RE = re.compile(r"\((?:[^)]*?\s+)?(\d{4})\)")

# Split the citation at the volume number that precedes the reporter
# e.g. "Brown v. Board, 347 U.S. 483 (1954)" → split before ", 347 "
_REPORTER_SPLIT_RE = re.compile(r",\s+\d+\s+")

# Token splitter: whitespace or hyphens
_TOKEN_SPLIT_RE = re.compile(r"[\s\-]+")

_STRIP_CHARS = ".,;:()'\"[]"

_STOP_WORDS = frozenset(
    {
        "am",
        "the",
        "of",
        "in",
        "a",
        "an",
        "and",
        "or",
        "for",
        "to",
        "at",
        "by",
        "on",
        "vs",
        "inc",
        "corp",
        "co",
        "ltd",
        "llc",
    }
)


# ── Context extraction ──────────────────────────────────────────────────────


def extract_year(text: str) -> str | None:
    """Return the 4-digit year from a citation parenthetical, or None."""
    m = _YEAR_RE.search(text)
    return m.group(1) if m else None


def extract_court_id(text: str) -> str | None:
    """Return a CourtListener court_id inferred from reporter/parenthetical."""
    if _SCOTUS_REPORTER_RE.search(text):
        return "scotus"
    if _DC_CIR_RE.search(text):
        return "cadc"
    if _FED_CIR_RE.search(text):
        return "cafc"
    m = _CIRCUIT_RE.search(text)
    if m:
        return f"ca{int(m.group(1))}"
    return None


def extract_name_tokens(raw_text: str) -> list[str]:
    """Return meaningful words from the case-name portion of a citation.

    Strips the reporter+page portion, splits on 'v.', then tokenises each
    party name and filters out short words and stop words.
    """
    # Drop everything from the first ", <volume> " onward (start of reporter)
    parts_pre = _REPORTER_SPLIT_RE.split(raw_text, maxsplit=1)
    case_name_text = parts_pre[0]

    # Split on " v. " (case-insensitive)
    parties = re.split(r"\bv\.\s+", case_name_text, flags=re.IGNORECASE)

    tokens: list[str] = []
    for party in parties:
        for word in _TOKEN_SPLIT_RE.split(party):
            word = word.strip(_STRIP_CHARS)
            if len(word) >= 3 and word.lower() not in _STOP_WORDS and word.isalpha():
                tokens.append(word)

    # Deduplicate preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for t in tokens:
        tl = t.lower()
        if tl not in seen:
            seen.add(tl)
            unique.append(t)
    return unique


def extract_case_name_from_text(text: str) -> str | None:
    """Extract a 'Party v. Party' case name from raw citation text or a snippet.

    Returns the matched case name string (e.g. "Marshall v. Amuso"), or None.
    """
    m = _SNIPPET_CASE_NAME_RE.search(text)
    if not m:
        return None
    plaintiff = m.group(1).strip().rstrip(",;.")
    defendant = m.group(2).strip().rstrip(",;.")
    if not plaintiff or not defendant:
        return None
    return f"{plaintiff} v. {defendant}"


# ── Scoring ─────────────────────────────────────────────────────────────────


def score_candidate(
    candidate: dict,
    *,
    year: str | None,
    court_id: str | None,
    name_tokens: list[str],
    extracted_case_name: str | None = None,
) -> int:
    """Return a non-negative integer score for one candidate cluster.

    Scoring breakdown
    -----------------
    +3  Year in citation parenthetical matches candidate's date_filed year.
    +3  Court inferred from reporter/parenthetical matches candidate's court_id.
    +5  Full case name extracted from raw_text or snippet matches candidate's
        case_name with confidence >= 0.8 (abbreviation-aware via case_names_match).
    +1  Each individual name token found in candidate's case_name (legacy
        fallback; applies when no high-confidence full-name match was found).
    """
    score = 0

    # Year match (+3)
    if year:
        date_filed = candidate.get("date_filed") or ""
        if date_filed[:4] == year:
            score += 3

    # Court match (+3)
    if court_id:
        if candidate.get("court", "") == court_id:
            score += 3

    candidate_name = candidate.get("case_name") or ""

    # Full case-name match (+5) — abbreviation-aware
    if extracted_case_name:
        matched, confidence = case_names_match(extracted_case_name, candidate_name)
        if matched and confidence >= 0.8:
            score += 5
            return score  # No need to add legacy token scores on top

    # Legacy token matches (+1 each) — fallback when full name not available
    candidate_name_lower = candidate_name.lower()
    for token in name_tokens:
        if token.lower() in candidate_name_lower:
            score += 1

    return score


# ── Selection ────────────────────────────────────────────────────────────────


def pick_winner(
    candidates: list[dict],
    *,
    year: str | None,
    court_id: str | None,
    name_tokens: list[str],
    extracted_case_name: str | None = None,
    min_score: int = MIN_SCORE,
    min_margin: int = MIN_MARGIN,
) -> dict | None:
    """Return the best candidate if it clears both thresholds, else None."""
    if not candidates:
        return None

    scored = sorted(
        [
            (
                score_candidate(
                    c,
                    year=year,
                    court_id=court_id,
                    name_tokens=name_tokens,
                    extracted_case_name=extracted_case_name,
                ),
                c,
            )
            for c in candidates
        ],
        key=lambda x: x[0],
        reverse=True,
    )

    best_score, best_candidate = scored[0]
    logger.debug(
        "Heuristic scores: %s",
        [(s, c.get("cluster_id")) for s, c in scored],
    )

    if best_score < min_score:
        logger.debug("Heuristic: best score %d below min_score %d", best_score, min_score)
        return None

    if len(scored) > 1:
        second_score = scored[1][0]
        if best_score - second_score < min_margin:
            logger.debug(
                "Heuristic: margin %d below min_margin %d (scores %d vs %d)",
                best_score - second_score,
                min_margin,
                best_score,
                second_score,
            )
            return None

    return best_candidate


# ── Public entry point ───────────────────────────────────────────────────────


def try_heuristic_resolution(
    raw_text: str,
    snippet: str | None,
    candidate_metadata: list[dict],
) -> dict | None:
    """Attempt to auto-select a candidate using context clues.

    Parameters
    ----------
    raw_text:
        The raw citation text (e.g. "Brown v. Board, 347 U.S. 483 (1954)").
    snippet:
        Surrounding document text near the citation (may be None).
    candidate_metadata:
        List of dicts with keys: cluster_id, case_name, court, date_filed.

    Returns
    -------
    The winning candidate dict, or None if no candidate wins clearly.
    """
    year = extract_year(raw_text)
    court_id = extract_court_id(raw_text)
    name_tokens = extract_name_tokens(raw_text)

    # Try to extract a full "X v. Y" case name for high-confidence matching.
    # Prefer the snippet (wider context) then fall back to raw_text.
    extracted_case_name: str | None = None
    for source in (snippet, raw_text):
        if source:
            extracted_case_name = extract_case_name_from_text(source)
            if extracted_case_name:
                break

    logger.debug(
        "Heuristic context for %r: year=%r court_id=%r tokens=%r case_name=%r",
        raw_text,
        year,
        court_id,
        name_tokens,
        extracted_case_name,
    )

    return pick_winner(
        candidate_metadata,
        year=year,
        court_id=court_id,
        name_tokens=name_tokens,
        extracted_case_name=extracted_case_name,
    )
