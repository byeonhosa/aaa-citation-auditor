"""Harvard Caselaw Access Project (CAP) verification fallback.

Provides a secondary case-law lookup when CourtListener fails to find a
citation.  Queries the CAP REST API (api.case.law/v1/cases/) by normalized
citation string, then by case name if the first query returns nothing.

Graceful degradation
--------------------
The CAP API was fully decommissioned in 2024.  All requests to
api.case.law/v1/ now return an HTTP 301 redirect to a documentation page
(which is HTML, not JSON).  This module detects that situation — and any
other connectivity or parse failure — and returns ``None`` so the pipeline
continues without change.  A one-time warning is logged on the first
unavailable response so the operator can see CAP is unreachable without
flooding the log on every citation.

If the API is ever restored, the same code will resume working automatically.

Resolution rules
----------------
* 1 result  → VERIFIED (resolution_method="cap_fallback")
* 2–5 results → AMBIGUOUS (caller's heuristic pass may further resolve)
* 0 results → None (leave NOT_FOUND)
* API unavailable / non-JSON / error → None (leave NOT_FOUND)
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.services.verification import VerificationResponse

logger = logging.getLogger(__name__)

# CAP base URL for the (now-decommissioned) v1 API
_CAP_BASE_URL = "https://api.case.law/v1"

# Maximum candidates to surface as AMBIGUOUS; anything above is too broad
_MAX_USEFUL_RESULTS = 5

# Track whether we have already logged an "unavailable" warning so the log
# is not spammed on every citation when the API is down.
_cap_unavailable_warned: bool = False


def _warn_once(message: str) -> None:
    global _cap_unavailable_warned  # noqa: PLW0603
    if not _cap_unavailable_warned:
        logger.warning(message)
        _cap_unavailable_warned = True


def _get_json(url: str, params: dict[str, Any], timeout: int) -> dict | None:
    """Perform a GET request and return parsed JSON, or None on any failure.

    Returns None when:
    - The response is not JSON (e.g. an HTML redirect page)
    - An HTTP error, timeout, or connection failure occurs
    - The JSON is not a dict (unexpected top-level type)
    """
    try:
        response = httpx.get(url, params=params, timeout=float(timeout), follow_redirects=False)
    except httpx.TimeoutException:
        _warn_once(
            "CAP API request timed out — treating CAP as unavailable for this run."
        )
        return None
    except httpx.RequestError as exc:
        _warn_once(
            f"CAP API connection error ({exc}) — treating CAP as unavailable for this run."
        )
        return None

    # Redirect (301/302) means the API is decommissioned; the target is an HTML page.
    if response.is_redirect or response.status_code in (301, 302, 303, 307, 308):
        _warn_once(
            f"CAP API returned a redirect (HTTP {response.status_code}) — the API appears to be "
            "decommissioned. CAP fallback disabled for this run."
        )
        return None

    if not response.is_success:
        _warn_once(
            f"CAP API returned HTTP {response.status_code} — treating CAP as unavailable."
        )
        return None

    content_type = response.headers.get("content-type", "")
    if "json" not in content_type:
        _warn_once(
            f"CAP API returned non-JSON content-type ({content_type!r}) — "
            "API may be decommissioned. CAP fallback disabled for this run."
        )
        return None

    try:
        data = response.json()
    except Exception:
        _warn_once("CAP API response could not be parsed as JSON — CAP fallback disabled.")
        return None

    if not isinstance(data, dict):
        _warn_once("CAP API returned unexpected JSON structure — CAP fallback disabled.")
        return None

    return data


def _parse_results(data: dict) -> list[dict]:
    """Extract candidate dicts from a CAP API response body."""
    results = data.get("results")
    if not isinstance(results, list):
        return []

    candidates: list[dict] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        cluster_id = item.get("id")
        if cluster_id is None:
            continue
        case_name = item.get("name_abbreviation") or item.get("name") or ""
        court_info = item.get("court") or {}
        court = court_info.get("name_abbreviation") or court_info.get("name") or ""
        date_filed = (item.get("decision_date") or "")[:10]
        citations_list = item.get("citations") or []
        cite_str = citations_list[0].get("cite", "") if citations_list else ""
        candidates.append(
            {
                "cluster_id": cluster_id,
                "case_name": case_name,
                "court": court,
                "date_filed": date_filed,
                "citation": cite_str,
            }
        )
    return candidates


def _build_response(candidates: list[dict], query_desc: str) -> VerificationResponse | None:
    """Convert a candidate list to a VerificationResponse, or None if no results."""
    if not candidates:
        return None

    cluster_ids = [c["cluster_id"] for c in candidates]

    if len(candidates) == 1:
        winner = candidates[0]
        case_name = winner.get("case_name") or ""
        detail = f"Found via CAP cite lookup ({query_desc})"
        if case_name:
            detail += f". {case_name}"
        return VerificationResponse(
            status="VERIFIED",
            detail=detail + ".",
            candidate_cluster_ids=cluster_ids,
            candidate_metadata=candidates,
        )

    return VerificationResponse(
        status="AMBIGUOUS",
        detail=f"Multiple CAP results for {query_desc} ({len(candidates)} candidates).",
        candidate_cluster_ids=cluster_ids,
        candidate_metadata=candidates,
    )


class CAPVerifier:
    """Queries the Harvard CAP API as a secondary case-law fallback.

    Parameters
    ----------
    api_key:
        Optional CAP API key. The free tier allows limited anonymous requests;
        an API key permits higher-volume access. May be None.
    timeout_seconds:
        HTTP request timeout in seconds.
    base_url:
        CAP API base URL. Overridable for testing.
    """

    def __init__(
        self,
        api_key: str | None = None,
        timeout_seconds: int = 15,
        base_url: str = _CAP_BASE_URL,
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._base_url = base_url.rstrip("/")

    def _params(self, extra: dict[str, Any]) -> dict[str, Any]:
        params: dict[str, Any] = {**extra}
        if self._api_key:
            params["api_key"] = self._api_key
        return params

    def lookup_by_cite(self, normalized_cite: str) -> VerificationResponse | None:
        """Look up a citation by its normalized reporter string (e.g. '347 U.S. 483').

        Returns a VerificationResponse or None if not found / unavailable.
        """
        url = f"{self._base_url}/cases/"
        params = self._params({"cite": normalized_cite})
        data = _get_json(url, params, self._timeout)
        if data is None:
            return None

        count = data.get("count", 0)
        if count == 0:
            return None

        candidates = _parse_results(data)[:_MAX_USEFUL_RESULTS]
        if not candidates:
            return None

        logger.debug(
            "CAP cite lookup %r: count=%d, using %d candidate(s)",
            normalized_cite,
            count,
            len(candidates),
        )
        return _build_response(candidates, f"cite={normalized_cite!r}")

    def lookup_by_name(self, case_name: str) -> VerificationResponse | None:
        """Look up a citation by case name (e.g. 'Brown v. Board of Education').

        Returns a VerificationResponse or None if not found / unavailable.
        """
        url = f"{self._base_url}/cases/"
        params = self._params({"name_abbreviation": case_name})
        data = _get_json(url, params, self._timeout)
        if data is None:
            return None

        count = data.get("count", 0)
        if count == 0:
            return None

        candidates = _parse_results(data)[:_MAX_USEFUL_RESULTS]
        if not candidates:
            return None

        logger.debug(
            "CAP name lookup %r: count=%d, using %d candidate(s)",
            case_name,
            count,
            len(candidates),
        )
        return _build_response(candidates, f"name={case_name!r}")

    def verify_citation(
        self,
        normalized_cite: str,
        case_name: str | None = None,
    ) -> VerificationResponse | None:
        """Try cite lookup first, then name lookup if cite returns nothing.

        Returns a VerificationResponse or None if not found / unavailable.
        """
        result = self.lookup_by_cite(normalized_cite)
        if result is not None:
            return result

        if case_name:
            result = self.lookup_by_name(case_name)

        return result
