"""Heuristic auto-disambiguation for AMBIGUOUS CourtListener results.

When CourtListener returns multiple candidate clusters for a citation, this
module tries to pick the correct one using context clues extracted from the
citation text and surrounding snippet.

Scoring rules
-------------
+3  Year in parenthetical matches candidate's date_filed year
+3  Court in parenthetical matches candidate's court_id
+1  Each case-name token found in candidate's case_name (case-insensitive)

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

logger = logging.getLogger(__name__)

# ── Scoring thresholds ──────────────────────────────────────────────────────

MIN_SCORE = 3
MIN_MARGIN = 2

# ── Compiled patterns ───────────────────────────────────────────────────────

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


# ── Scoring ─────────────────────────────────────────────────────────────────


def score_candidate(
    candidate: dict,
    *,
    year: str | None,
    court_id: str | None,
    name_tokens: list[str],
) -> int:
    """Return a non-negative integer score for one candidate cluster."""
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

    # Case-name token matches (+1 each)
    candidate_name = (candidate.get("case_name") or "").lower()
    for token in name_tokens:
        if token.lower() in candidate_name:
            score += 1

    return score


# ── Selection ────────────────────────────────────────────────────────────────


def pick_winner(
    candidates: list[dict],
    *,
    year: str | None,
    court_id: str | None,
    name_tokens: list[str],
    min_score: int = MIN_SCORE,
    min_margin: int = MIN_MARGIN,
) -> dict | None:
    """Return the best candidate if it clears both thresholds, else None."""
    if not candidates:
        return None

    scored = sorted(
        [
            (score_candidate(c, year=year, court_id=court_id, name_tokens=name_tokens), c)
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
    # Use raw_text as primary source; snippet adds no extra clues for
    # year/court (those are in the citation itself) but could help with
    # case-name tokens if the raw text is very abbreviated.
    year = extract_year(raw_text)  # year from the citation itself
    court_id = extract_court_id(raw_text)
    name_tokens = extract_name_tokens(raw_text)

    logger.debug(
        "Heuristic context for %r: year=%r court_id=%r tokens=%r",
        raw_text,
        year,
        court_id,
        name_tokens,
    )

    return pick_winner(
        candidate_metadata,
        year=year,
        court_id=court_id,
        name_tokens=name_tokens,
    )
