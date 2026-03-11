"""CourtListener search-API fallback for NOT_FOUND citations.

When the citation-lookup API returns NOT_FOUND, this module tries a keyword
search against the CourtListener opinions search endpoint.  This is useful for
citations using proprietary reporter systems (LEXIS, WL) or unusual formats
that the citation-lookup API doesn't recognise.

Search endpoint: GET /api/rest/v4/search/?type=o&q=<query>

Multi-strategy approach
-----------------------
Strategies are tried in order, most-precise first.  As soon as one returns
1–3 results the search stops (max 3 API calls total).

Strategy 1  Case name + year + CourtListener court_id extracted from text.
Strategy 2  Case name + raw court abbreviation from parenthetical text
            (e.g. "6th Cir.", "D. Me."), if different from Strategy 1.
Strategy 3  Last names of the parties only (broadest — most likely to match).

Resolution rules (per strategy)
--------------------------------
• 0 results  → try next strategy
• 1 result   → VERIFIED with resolution_method="search_fallback"
• 2-3 results → AMBIGUOUS (caller's heuristic pass may further resolve)
• 4+ results → try next strategy (query still too broad)
• No case name extracted → skip to token-based fallback
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from app.services.audit import CitationResult
from app.services.disambiguation import extract_court_id, extract_name_tokens, extract_year
from app.services.verification import VerificationResponse

logger = logging.getLogger(__name__)

# CourtListener search endpoint for opinions
COURTLISTENER_SEARCH_URL = "https://www.courtlistener.com/api/rest/v4/search/"

# Courtesy delay between fallback requests to avoid hammering the API
_FALLBACK_DELAY_SECONDS = 0.5

# Maximum candidates to surface: 2-3 results → AMBIGUOUS, 4+ → too broad
_MAX_USEFUL_RESULTS = 3

# Pattern to extract "Party v. Party" case name from surrounding text
_CASE_NAME_RE = re.compile(
    r"\b([A-Z][A-Za-z''\-\s]{1,40})\s+v\.\s+([A-Z][A-Za-z''\-\s]{1,40}?)(?=\s*[,;(]|\s*\d|\Z)",
)

# Pattern to extract court abbreviation from a parenthetical like "(6th Cir. 2003)"
# or "(D. Me. 1998)".  Captures the non-year portion before the 4-digit year.
# The first char may be a digit (e.g. "6th Cir.", "2d Cir.").
_COURT_ABBR_RE = re.compile(r"\(([A-Za-z0-9][A-Za-z0-9.\s]{1,25}?)\s+\d{4}\)")


# ── Query building ────────────────────────────────────────────────────────────


def _extract_case_name_from_snippet(snippet: str) -> str | None:
    """Extract a 'X v. Y' case name from the surrounding snippet text.

    Returns the full matched case name string (e.g. "Marshall v. Amuso"),
    or None if no case name pattern is found.
    """
    m = _CASE_NAME_RE.search(snippet)
    if not m:
        return None
    # Combine both parties, strip trailing whitespace
    plaintiff = m.group(1).strip().rstrip(",;")
    defendant = m.group(2).strip().rstrip(",;")
    if not plaintiff or not defendant:
        return None
    return f"{plaintiff} v. {defendant}"


def _extract_court_abbr_from_text(text: str) -> str | None:
    """Extract a raw court abbreviation from a citation parenthetical.

    E.g. "(6th Cir. 2003)" → "6th Cir.", "(D. Me. 1998)" → "D. Me.".
    Returns None if no court abbreviation is found or the match looks like
    a bare year-only parenthetical.
    """
    m = _COURT_ABBR_RE.search(text)
    if not m:
        return None
    abbr = m.group(1).strip()
    # Filter out single-word noise like "en" or bare ordinals
    if len(abbr) < 4 or abbr.lower() in {"en banc", "reh'g"}:
        return None
    return abbr


def _extract_last_names(case_name: str) -> str | None:
    """Return the first significant word from each party name.

    "Bourne v. Arruda" → "Bourne Arruda"
    "United States v. Jones Corp." → "United Jones"

    Returns None if fewer than two words can be extracted.
    """
    parts = re.split(r"\bv\.\s+", case_name, flags=re.IGNORECASE)
    if len(parts) < 2:
        return None

    last_names: list[str] = []
    for party in parts:
        first_word = party.strip().split()[0].rstrip(".,;") if party.strip() else ""
        if len(first_word) >= 3:
            last_names.append(first_word)

    return " ".join(last_names) if len(last_names) >= 2 else None


def _build_strategies(citation: CitationResult) -> list[str]:
    """Return an ordered list of search queries to try, most-precise first.

    Each string is a distinct query; duplicates are suppressed.  At most 3
    strategies are returned so the caller makes at most 3 API calls.
    """
    strategies: list[str] = []
    seen: set[str] = set()

    def _add(q: str | None) -> None:
        if q and q.strip() and q not in seen:
            seen.add(q)
            strategies.append(q)

    # Determine the best case name available
    case_name: str | None = None
    for src in (citation.snippet, citation.raw_text):
        if src:
            case_name = _extract_case_name_from_snippet(src)
            if case_name:
                break

    # Contextual metadata
    year = None
    court_id = None
    court_abbr = None
    for src in (citation.snippet, citation.raw_text):
        if src:
            if not year:
                year = extract_year(src)
            if not court_id:
                court_id = extract_court_id(src)
            if not court_abbr:
                court_abbr = _extract_court_abbr_from_text(src)

    if case_name:
        # Strategy 1: case name + year + CourtListener court_id
        extras_1: list[str] = []
        if year:
            extras_1.append(year)
        if court_id:
            extras_1.append(court_id)
        q1 = case_name + (" " + " ".join(extras_1) if extras_1 else "")
        _add(q1)

        # Strategy 2: case name + raw court abbreviation (if distinct from S1)
        if court_abbr:
            extras_2: list[str] = [court_abbr]
            if year:
                extras_2.append(year)
            q2 = case_name + " " + " ".join(extras_2)
            _add(q2)

        # Strategy 3: last names only (broadest)
        _add(_extract_last_names(case_name))

    else:
        # Fallback when no "X v. Y" pattern found: use raw name tokens
        tokens = extract_name_tokens(citation.raw_text)
        if not tokens and citation.snippet:
            tokens = extract_name_tokens(citation.snippet)
        if tokens:
            _add(" ".join(tokens[:5]))

    return strategies[:3]


# ── HTTP call ─────────────────────────────────────────────────────────────────


def _search_courtlistener(
    query: str,
    *,
    token: str,
    search_url: str = COURTLISTENER_SEARCH_URL,
    timeout_seconds: int = 30,
) -> dict[str, Any] | None:
    """GET the CourtListener search API.  Returns parsed JSON or None on failure."""
    headers = {
        "Authorization": f"Token {token}",
        "Accept": "application/json",
    }
    params = {"type": "o", "q": query}

    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.get(search_url, params=params, headers=headers)
    except Exception:
        logger.exception("CourtListener search request failed for query: %r", query)
        return None

    if response.status_code == 429:
        logger.warning("CourtListener search: rate limited (429) for query: %r", query)
        return None
    if response.status_code >= 400:
        logger.warning(
            "CourtListener search: HTTP %d for query: %r",
            response.status_code,
            query,
        )
        return None

    try:
        return response.json()
    except Exception:
        logger.warning("CourtListener search: could not parse response for query: %r", query)
        return None


# ── Candidate extraction ──────────────────────────────────────────────────────


def _candidate_from_search_result(result: dict) -> dict | None:
    """Convert a CourtListener search result dict to a candidate metadata dict."""
    # The search API may use camelCase or snake_case depending on version
    cluster_id = result.get("cluster_id") or result.get("id")
    if not isinstance(cluster_id, int):
        return None
    return {
        "cluster_id": cluster_id,
        "case_name": result.get("caseName") or result.get("case_name") or "",
        "court": result.get("court_id") or result.get("court") or "",
        "date_filed": result.get("dateFiled") or result.get("date_filed") or "",
    }


def _evaluate_search_response(
    payload: dict[str, Any],
    citation: CitationResult,
    query: str,
) -> VerificationResponse | None:
    """Convert a raw CourtListener search payload into a VerificationResponse.

    Returns None if the result count is 0 or too large (≥ 4), signalling the
    caller to try the next strategy.  Returns VERIFIED for a single match,
    or AMBIGUOUS for 2–3 matches.
    """
    raw_results = payload.get("results") or []
    count = payload.get("count", 0)

    logger.info(
        "Search fallback result: query=%r → total_count=%d, returned=%d",
        query,
        count,
        len(raw_results),
    )

    # Too many results → query too broad; try next strategy
    if count > _MAX_USEFUL_RESULTS:
        logger.info(
            "Search fallback: query too broad (%d total results) for %r — trying next strategy",
            count,
            citation.raw_text,
        )
        return None

    candidates = [
        c
        for r in raw_results[:_MAX_USEFUL_RESULTS]
        if isinstance(r, dict)
        for c in [_candidate_from_search_result(r)]
        if c is not None
    ]

    if not candidates:
        logger.info("Search fallback: no usable candidates for %r", citation.raw_text)
        return None

    if len(candidates) == 1:
        winner = candidates[0]
        cid = winner["cluster_id"]
        case_name = winner.get("case_name") or ""
        detail = f"Resolved via search fallback (cluster {cid})"
        if case_name:
            detail += f". {case_name}"
        logger.info(
            "Search fallback auto-resolved: %r → cluster %d (%s)",
            citation.raw_text,
            cid,
            case_name,
        )
        return VerificationResponse(
            status="VERIFIED",
            detail=detail + ".",
            candidate_cluster_ids=[cid],
            candidate_metadata=candidates,
        )

    # 2–3 candidates — surface them for user disambiguation
    cluster_ids = [c["cluster_id"] for c in candidates if isinstance(c["cluster_id"], int)]
    logger.info(
        "Search fallback: %d candidates for %r → AMBIGUOUS",
        len(candidates),
        citation.raw_text,
    )
    return VerificationResponse(
        status="AMBIGUOUS",
        detail=f"Search found {len(candidates)} possible matches.",
        candidate_cluster_ids=cluster_ids or None,
        candidate_metadata=candidates,
    )


# ── Public entry point ────────────────────────────────────────────────────────


def try_search_fallback(
    citation: CitationResult,
    *,
    token: str,
    search_url: str = COURTLISTENER_SEARCH_URL,
    timeout_seconds: int = 30,
) -> VerificationResponse | None:
    """Attempt to resolve a NOT_FOUND citation via the CourtListener search API.

    Tries up to three progressively-broader search strategies, stopping as
    soon as one returns 1–3 results.

    Parameters
    ----------
    citation:
        The citation whose status is NOT_FOUND.
    token:
        CourtListener API token.
    search_url:
        Override the search endpoint (useful in tests).
    timeout_seconds:
        HTTP timeout per search request.

    Returns
    -------
    ``VerificationResponse`` with status VERIFIED (single unambiguous match)
    or AMBIGUOUS (2–3 candidates), or ``None`` if all strategies fail
    (caller should keep NOT_FOUND).
    """
    strategies = _build_strategies(citation)
    if not strategies:
        logger.debug("Search fallback skipped: no query tokens for %r", citation.raw_text)
        return None

    for query in strategies:
        # Courtesy delay to respect CourtListener's rate limits
        time.sleep(_FALLBACK_DELAY_SECONDS)

        logger.info(
            "Search fallback: querying CourtListener for %r (query=%r)",
            citation.raw_text,
            query,
        )

        payload = _search_courtlistener(
            query, token=token, search_url=search_url, timeout_seconds=timeout_seconds
        )
        if payload is None:
            continue

        result = _evaluate_search_response(payload, citation, query)
        if result is not None:
            return result

    return None
