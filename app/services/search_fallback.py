"""CourtListener search-API fallback for NOT_FOUND citations.

When the citation-lookup API returns NOT_FOUND, this module tries a keyword
search against the CourtListener opinions search endpoint.  This is useful for
citations using proprietary reporter systems (LEXIS, WL) or unusual formats
that the citation-lookup API doesn't recognise.

Search endpoint: GET /api/rest/v4/search/?type=o&q=<query>

Resolution rules
----------------
• 0 results  → return None (caller keeps NOT_FOUND)
• 1 result   → VERIFIED with resolution_method="search_fallback"
• 2+ results → AMBIGUOUS (caller's heuristic pass may further resolve)
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.services.audit import CitationResult
from app.services.disambiguation import extract_name_tokens
from app.services.verification import VerificationResponse

logger = logging.getLogger(__name__)

# CourtListener search endpoint for opinions
COURTLISTENER_SEARCH_URL = "https://www.courtlistener.com/api/rest/v4/search/"

# Courtesy delay between fallback requests to avoid hammering the API
_FALLBACK_DELAY_SECONDS = 0.5

# Maximum candidates to surface from search results
_MAX_SEARCH_CANDIDATES = 5


# ── Query building ────────────────────────────────────────────────────────────


def _build_search_query(citation: CitationResult) -> str | None:
    """Build a CourtListener search query from a citation's text and snippet.

    For LEXIS/WL citations the raw text has no useful case-name tokens; in
    that case we fall back to the surrounding snippet which often contains
    the case name.
    """
    tokens = extract_name_tokens(citation.raw_text)
    if not tokens and citation.snippet:
        tokens = extract_name_tokens(citation.snippet)
    if not tokens:
        return None
    # Use up to 5 meaningful tokens to keep the query focused
    return " ".join(tokens[:5])


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


# ── Public entry point ────────────────────────────────────────────────────────


def try_search_fallback(
    citation: CitationResult,
    *,
    token: str,
    search_url: str = COURTLISTENER_SEARCH_URL,
    timeout_seconds: int = 30,
) -> VerificationResponse | None:
    """Attempt to resolve a NOT_FOUND citation via the CourtListener search API.

    Parameters
    ----------
    citation:
        The citation whose status is NOT_FOUND.
    token:
        CourtListener API token.
    search_url:
        Override the search endpoint (useful in tests).
    timeout_seconds:
        HTTP timeout for the search request.

    Returns
    -------
    ``VerificationResponse`` with status VERIFIED (single unambiguous match)
    or AMBIGUOUS (multiple candidates), or ``None`` if no results were found
    (caller should keep NOT_FOUND).
    """
    query = _build_search_query(citation)
    if not query:
        logger.debug("Search fallback skipped: no query tokens for %r", citation.raw_text)
        return None

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
        return None

    raw_results = payload.get("results") or []
    count = payload.get("count", 0)

    logger.info(
        "Search fallback result: query=%r → total_count=%d, returned=%d",
        query,
        count,
        len(raw_results),
    )

    candidates = [
        c
        for r in raw_results[:_MAX_SEARCH_CANDIDATES]
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

    # Multiple candidates — surface them for user disambiguation
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
