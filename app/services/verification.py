from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.services.audit import CitationResult
from app.services.disambiguation import try_heuristic_resolution
from app.services.http_client import post_with_retry
from app.services.name_matching import case_names_match
from app.services.statute_verification import (
    FederalStatuteVerifier,
    VirginiaStatuteVerifier,
    parse_federal_section,
    parse_virginia_section,
)

# ── Short-citation matching helpers ──────────────────────────────────────────

# Matches short citations like "588 U.S. at 392" or "123 F.3d at 456".
# Reporter group uses [\w.]+ to handle reporters with embedded digits (F.3d,
# S.Ct., L.Ed.2d) as well as plain dotted reporters (U.S.).
_SHORT_CITE_RE = re.compile(r"^(\d+)\s+([\w.]+)\s+at\s+\d+")

# Matches full citations like "588 U.S. 388" or "123 F.3d 400".
_FULL_CITE_REPORTER_RE = re.compile(r"^(\d+)\s+([\w.]+)\s+\d+")


def _parse_volume_reporter(raw_text: str) -> tuple[str, str] | None:
    """Return (volume, reporter) from a citation's raw text, or None.

    Works for both short citations ("588 U.S. at 392") and full citations
    ("588 U.S. 388").  Reporter comparison is case-insensitive.
    """
    for pattern in (_SHORT_CITE_RE, _FULL_CITE_REPORTER_RE):
        m = pattern.match(raw_text.strip())
        if m:
            volume = m.group(1)
            reporter = m.group(2).strip().rstrip(".")
            return volume, reporter.lower()
    return None


logger = logging.getLogger(__name__)

# ── Rate-limit detail constant ────────────────────────────────────────────────
_RATE_LIMITED_DETAIL = (
    "Verification temporarily rate-limited. "
    "Try again later or these will be verified on your next audit via the resolution cache."
)

# ── Parallel citation adjacency helpers ──────────────────────────────────────

# Only punctuation/whitespace/dashes between two parallel citations.
_PARALLEL_SEP_RE = re.compile(r"^[\s,.()\u2013\u2014\-]*$")


def _is_parallel_adjacent(text_a: str, text_b: str, snippet: str) -> bool:
    """Return True if text_a and text_b appear as adjacent parallel citations.

    Conservative: requires the two texts to be separated by at most 12 characters
    of punctuation/whitespace only (no alphabetic words) inside *snippet*.
    """
    lower = snippet.lower()
    a_pos = lower.find(text_a.lower())
    b_pos = lower.find(text_b.lower())
    if a_pos == -1 or b_pos == -1:
        return False
    if a_pos < b_pos:
        between = snippet[a_pos + len(text_a) : b_pos]
    else:
        between = snippet[b_pos + len(text_b) : a_pos]
    return len(between) <= 12 and bool(_PARALLEL_SEP_RE.match(between))


def _resolve_parallel_citations(citations: list[CitationResult]) -> int:
    """Seventh pass: resolve NOT_FOUND/AMBIGUOUS citations adjacent to a VERIFIED one.

    When two case-law citations appear side-by-side in the source text (separated
    only by a comma and optional whitespace), the NOT_FOUND citation likely represents
    the same case in a parallel reporter.  It inherits the VERIFIED citation's
    cluster metadata.

    Returns the count of citations resolved.
    """
    verified_with_snippet = [
        c
        for c in citations
        if c.verification_status == "VERIFIED"
        and c.snippet
        and c.citation_type != "FullLawCitation"
    ]
    if not verified_with_snippet:
        return 0

    resolved = 0
    for citation in citations:
        if citation.verification_status not in ("NOT_FOUND", "AMBIGUOUS"):
            continue
        if citation.citation_type == "FullLawCitation":
            continue
        if not citation.snippet:
            continue

        partner: CitationResult | None = None
        for v in verified_with_snippet:
            # Check this citation's snippet for the verified citation's text
            if _is_parallel_adjacent(citation.raw_text, v.raw_text, citation.snippet):
                partner = v
                break
            # Also check the verified citation's snippet
            if v.snippet and _is_parallel_adjacent(citation.raw_text, v.raw_text, v.snippet):
                partner = v
                break

        if partner is None:
            continue

        case_name = ""
        if partner.candidate_metadata:
            case_name = partner.candidate_metadata[0].get("case_name") or ""

        citation.verification_status = "VERIFIED"
        citation.selected_cluster_id = partner.selected_cluster_id
        citation.candidate_cluster_ids = partner.candidate_cluster_ids
        citation.candidate_metadata = partner.candidate_metadata
        citation.resolution_method = "parallel_cite"
        detail = f"Identified as parallel reporter for {partner.raw_text!r}"
        if case_name:
            detail += f" ({case_name})"
        citation.verification_detail = detail + "."
        resolved += 1
        logger.info(
            "Parallel cite resolved: %r → cluster %s (partner: %r)",
            citation.raw_text,
            partner.selected_cluster_id,
            partner.raw_text,
        )

    return resolved


# ── Candidate deduplication ───────────────────────────────────────────────────


def _deduplicate_candidates(
    candidates: list[dict],
) -> tuple[list[dict], bool]:
    """Deduplicate CourtListener candidates by fuzzy case_name + date_filed.

    CourtListener occasionally returns multiple cluster IDs for the same
    opinion (a data issue on their side).  Deduplication keeps the first
    entry encountered within each (name-match, date) group.

    Name comparison uses ``case_names_match`` from name_matching so that
    abbreviation variants (e.g. "Dept." vs "Department") are treated as
    the same name.

    Returns
    -------
    (deduped_list, had_duplicates)
    """
    kept: list[dict] = []
    had_duplicates = False

    for candidate in candidates:
        cand_name = candidate.get("case_name") or ""
        cand_date = (candidate.get("date_filed") or "")[:10]  # YYYY-MM-DD or empty

        # Check if this candidate matches any already-kept entry
        is_dup = False
        for existing in kept:
            existing_name = existing.get("case_name") or ""
            existing_date = (existing.get("date_filed") or "")[:10]
            if existing_date == cand_date:
                matched, confidence = case_names_match(cand_name, existing_name)
                if matched and confidence >= 0.8:
                    is_dup = True
                    existing_id = existing.get("cluster_id")
                    new_id = candidate.get("cluster_id")
                    logger.warning(
                        "Duplicate CourtListener candidate: cluster %s duplicates cluster %s "
                        "(case_name=%r ~ %r, date=%r, confidence=%.1f) — "
                        "likely a CourtListener data issue; please report to their team.",
                        new_id,
                        existing_id,
                        cand_name,
                        existing_name,
                        cand_date,
                        confidence,
                    )
                    break

        if is_dup:
            had_duplicates = True
        else:
            kept.append(candidate)

    return kept, had_duplicates


@dataclass
class VerificationResponse:
    status: str
    detail: str
    candidate_cluster_ids: list[int] | None = None
    candidate_metadata: list[dict] | None = None


class CitationVerifier(Protocol):
    def verify(self, citation: CitationResult) -> VerificationResponse: ...


STATUTE_CITATION_TYPES = frozenset({"FullLawCitation"})

BATCH_MAX_CITATIONS = 250
BATCH_MAX_TEXT_BYTES = 60_000  # leave margin from 64K API limit


def is_statute_citation(citation: CitationResult) -> bool:
    return citation.citation_type in STATUTE_CITATION_TYPES


def is_derived_citation(citation: CitationResult) -> bool:
    return citation.raw_text.lower().startswith("id.") or citation.citation_type.lower().startswith(
        "id"
    )


def is_supra_citation(citation: CitationResult) -> bool:
    return citation.citation_type == "SupraCitation"


def _update_derived_details(citations: list[CitationResult]) -> None:
    """Post-processing: update DERIVED citation detail text based on parent verification status.

    Called after all verification passes so each parent's final status is known.
    """
    parent_status: dict[str, str] = {
        c.raw_text: (c.verification_status or "")
        for c in citations
        if c.raw_text and c.verification_status != "DERIVED"
    }
    for c in citations:
        if c.verification_status != "DERIVED":
            continue
        is_supra = c.resolution_method == "supra_ref"
        parent_raw = c.resolved_from
        if not parent_raw:
            c.verification_detail = "Derived citation — parent citation not identified."
            continue
        label = "Supra back-reference to" if is_supra else "Derived from"
        pstatus = parent_status.get(parent_raw, "")
        if pstatus == "VERIFIED":
            c.verification_detail = f"{label} {parent_raw} — parent citation verified."
        elif pstatus == "NOT_FOUND":
            c.verification_detail = f"{label} {parent_raw} — parent citation could not be verified."
        elif pstatus == "AMBIGUOUS":
            c.verification_detail = f"{label} {parent_raw} — parent citation is ambiguous."
        else:
            c.verification_detail = f"{label} {parent_raw} — parent status unknown."


def _postprocess_citations(citations: list[CitationResult]) -> None:
    """Run all post-verification updates that require the full citation list."""
    _update_derived_details(citations)


def _lookup_text_for(citation: CitationResult) -> str:
    """Return the text to send to CourtListener for a single citation."""
    return citation.normalized_text or citation.raw_text or ""


def _split_into_batches(
    citations: list[CitationResult],
    max_count: int = BATCH_MAX_CITATIONS,
    max_text_bytes: int = BATCH_MAX_TEXT_BYTES,
) -> list[list[CitationResult]]:
    """Split citations into batches respecting count and text-size limits."""
    batches: list[list[CitationResult]] = []
    current_batch: list[CitationResult] = []
    current_size = 0
    separator_size = 1  # len("\n".encode("utf-8"))

    for citation in citations:
        text_size = len(_lookup_text_for(citation).encode("utf-8"))
        added_size = text_size + (separator_size if current_batch else 0)

        if current_batch and (
            len(current_batch) >= max_count or current_size + added_size > max_text_bytes
        ):
            batches.append(current_batch)
            current_batch = []
            current_size = 0
            added_size = text_size  # no separator for first item in new batch

        current_batch.append(citation)
        current_size += added_size

    if current_batch:
        batches.append(current_batch)

    return batches


def map_courtlistener_result(result: dict[str, Any]) -> VerificationResponse:
    status_code = result.get("status")
    clusters = result.get("clusters") if isinstance(result.get("clusters"), list) else []
    error_message = result.get("error_message")

    if status_code == 200:
        cluster_count = len(clusters)
        detail = "CourtListener matched citation."
        # Extract cluster metadata so the result can be cached.
        # Only treat as a definitive single match when exactly one cluster is
        # returned; multiple clusters on a 200 are treated as AMBIGUOUS so the
        # user (or heuristic) can select the correct one.
        candidate_metadata: list[dict] | None = None
        candidate_cluster_ids: list[int] | None = None
        if cluster_count == 1:
            cluster = clusters[0]
            if not isinstance(cluster, dict):
                return VerificationResponse(status="VERIFIED", detail=detail)
            cid = cluster.get("id")
            if isinstance(cid, int):
                candidate_metadata = [
                    {
                        "cluster_id": cid,
                        "case_name": cluster.get("case_name")
                        or cluster.get("case_name_short")
                        or "",
                        "court": cluster.get("court_id") or "",
                        "date_filed": cluster.get("date_filed") or "",
                    }
                ]
                candidate_cluster_ids = [cid]
            return VerificationResponse(
                status="VERIFIED",
                detail=detail,
                candidate_cluster_ids=candidate_cluster_ids,
                candidate_metadata=candidate_metadata,
            )
        elif cluster_count > 1:
            # Multiple clusters on a 200 response — surface as AMBIGUOUS
            raw_candidates: list[dict] = []
            for cluster in clusters:
                if not isinstance(cluster, dict):
                    continue
                cid = cluster.get("id")
                if isinstance(cid, int):
                    raw_candidates.append(
                        {
                            "cluster_id": cid,
                            "case_name": cluster.get("case_name")
                            or cluster.get("case_name_short")
                            or "",
                            "court": cluster.get("court_id") or "",
                            "date_filed": cluster.get("date_filed") or "",
                        }
                    )
            candidate_metadata, _ = _deduplicate_candidates(raw_candidates)
            candidate_cluster_ids = [c["cluster_id"] for c in candidate_metadata]
            return VerificationResponse(
                status="AMBIGUOUS",
                detail="Multiple possible CourtListener matches.",
                candidate_cluster_ids=candidate_cluster_ids or None,
                candidate_metadata=candidate_metadata or None,
            )
        # cluster_count == 0
        return VerificationResponse(
            status="VERIFIED",
            detail=detail,
            candidate_cluster_ids=None,
            candidate_metadata=None,
        )

    if status_code == 404:
        _not_found_msg = (
            "Citation not found in CourtListener."
            " It may be a recent opinion or one not yet indexed."
        )
        detail = str(error_message).strip() if error_message else _not_found_msg
        return VerificationResponse(status="NOT_FOUND", detail=detail)

    if status_code == 300:
        detail = (
            str(error_message).strip()
            if error_message
            else "Multiple possible CourtListener matches."
        )
        raw_candidates: list[dict] = []
        for cluster in clusters:
            if not isinstance(cluster, dict):
                continue
            cid = cluster.get("id")
            if isinstance(cid, int):
                raw_candidates.append(
                    {
                        "cluster_id": cid,
                        "case_name": cluster.get("case_name")
                        or cluster.get("case_name_short")
                        or "",
                        "court": cluster.get("court_id") or "",
                        "date_filed": cluster.get("date_filed") or "",
                    }
                )
        # Remove duplicate candidates (CourtListener data issue)
        candidate_metadata, _ = _deduplicate_candidates(raw_candidates)
        candidate_cluster_ids = [c["cluster_id"] for c in candidate_metadata]
        return VerificationResponse(
            status="AMBIGUOUS",
            detail=detail,
            candidate_cluster_ids=candidate_cluster_ids or None,
            candidate_metadata=candidate_metadata or None,
        )

    if status_code == 400:
        detail = (
            str(error_message).strip()
            if error_message
            else "CourtListener rejected citation lookup request."
        )
        return VerificationResponse(status="ERROR", detail=detail)

    if status_code == 429:
        return VerificationResponse(
            status="RATE_LIMITED",
            detail=_RATE_LIMITED_DETAIL,
        )

    if isinstance(status_code, int):
        return VerificationResponse(
            status="ERROR",
            detail=f"Unexpected CourtListener status: {status_code}.",
        )

    return VerificationResponse(
        status="ERROR", detail="Malformed CourtListener verification payload."
    )


class CourtListenerVerifier:
    def __init__(self, token: str, base_url: str, timeout_seconds: int = 30) -> None:
        self.token = token
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self._headers = {
            "Authorization": f"Token {self.token}",
            "Accept": "application/json",
        }

    # ── single-citation path ──────────────────────────────────────

    def verify(self, citation: CitationResult) -> VerificationResponse:
        lookup_text = _lookup_text_for(citation)
        if not lookup_text:
            return VerificationResponse(status="NOT_FOUND", detail="Citation text unavailable.")

        t0 = time.perf_counter()
        try:
            response = post_with_retry(
                self.base_url,
                data={"text": lookup_text},
                headers=self._headers,
                timeout_seconds=self.timeout_seconds,
            )
        except httpx.TimeoutException:
            logger.error(
                "CourtListener single request timed out for citation: %r", citation.raw_text
            )
            return VerificationResponse(
                status="ERROR",
                detail="CourtListener request timed out after retries.",
            )
        except Exception:
            logger.exception(
                "CourtListener single request failed for citation: %r", citation.raw_text
            )
            return VerificationResponse(status="ERROR", detail="Verification request failed.")

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.debug(
            "CourtListener single response: HTTP %d in %dms for citation: %r",
            response.status_code,
            elapsed_ms,
            citation.raw_text,
        )
        return self._handle_single_response(response)

    def _handle_single_response(self, response: httpx.Response) -> VerificationResponse:
        if response.status_code == 429:
            return VerificationResponse(
                status="RATE_LIMITED",
                detail=_RATE_LIMITED_DETAIL,
            )
        if response.status_code == 404:
            return VerificationResponse(
                status="NOT_FOUND",
                detail=(
                    "Citation not found in CourtListener."
                    " It may be a recent opinion or one not yet indexed."
                ),
            )
        if response.status_code == 400:
            return VerificationResponse(
                status="ERROR",
                detail="CourtListener rejected citation lookup request.",
            )
        if response.status_code >= 400:
            return VerificationResponse(
                status="ERROR",
                detail=f"Verification HTTP error: {response.status_code}.",
            )

        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError):
            return VerificationResponse(
                status="ERROR", detail="Could not parse verification response."
            )

        if not isinstance(payload, list) or not payload:
            return VerificationResponse(
                status="ERROR", detail="Unexpected CourtListener response shape."
            )

        first_result = payload[0]
        if not isinstance(first_result, dict):
            return VerificationResponse(
                status="ERROR", detail="Unexpected CourtListener result item."
            )

        return map_courtlistener_result(first_result)

    # ── batch path ────────────────────────────────────────────────

    def verify_batch(self, citations: list[CitationResult]) -> list[VerificationResponse]:
        """Verify multiple citations in a single API call.

        Concatenates citation lookup texts separated by newlines and sends one
        POST request.  Results are mapped back to citations by position.
        """
        if not citations:
            return []

        lookup_texts = [_lookup_text_for(c) for c in citations]
        combined_text = "\n".join(lookup_texts)

        t0 = time.perf_counter()
        logger.debug("CourtListener batch request: %d citations", len(citations))
        try:
            response = post_with_retry(
                self.base_url,
                data={"text": combined_text},
                headers=self._headers,
                timeout_seconds=self.timeout_seconds,
            )
        except httpx.TimeoutException:
            logger.error("CourtListener batch request timed out (%d citations)", len(citations))
            return [
                VerificationResponse(
                    status="ERROR",
                    detail="CourtListener batch request timed out after retries.",
                )
            ] * len(citations)
        except Exception:
            logger.exception("CourtListener batch request failed (%d citations)", len(citations))
            return [
                VerificationResponse(status="ERROR", detail="Batch verification request failed.")
            ] * len(citations)

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "CourtListener batch response: HTTP %d in %dms (%d citations)",
            response.status_code,
            elapsed_ms,
            len(citations),
        )
        return self._handle_batch_response(response, len(citations))

    def _handle_batch_response(
        self, response: httpx.Response, expected_count: int
    ) -> list[VerificationResponse]:
        if response.status_code == 429:
            logger.warning(
                "CourtListener rate limit hit (HTTP 429) — marking %d citation(s) as RATE_LIMITED",
                expected_count,
            )
            return [
                VerificationResponse(
                    status="RATE_LIMITED",
                    detail=_RATE_LIMITED_DETAIL,
                )
            ] * expected_count
        if response.status_code >= 400:
            return [
                VerificationResponse(
                    status="ERROR",
                    detail=f"Batch verification HTTP error: {response.status_code}.",
                )
            ] * expected_count

        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError):
            return [
                VerificationResponse(
                    status="ERROR",
                    detail="Could not parse batch verification response.",
                )
            ] * expected_count

        if not isinstance(payload, list):
            return [
                VerificationResponse(status="ERROR", detail="Unexpected batch response shape.")
            ] * expected_count

        # Map results by position (index-aligned with input)
        results: list[VerificationResponse] = []
        for i in range(expected_count):
            if i < len(payload):
                item = payload[i]
                if isinstance(item, dict):
                    results.append(map_courtlistener_result(item))
                else:
                    results.append(
                        VerificationResponse(status="ERROR", detail="Unexpected batch result item.")
                    )
            else:
                results.append(
                    VerificationResponse(
                        status="NOT_FOUND",
                        detail="Citation not included in batch response.",
                    )
                )

        return results


def _verify_single(
    verifiable: list[CitationResult],
    verifier: CitationVerifier,
) -> None:
    """Verify each citation individually (one HTTP call per citation)."""
    for citation in verifiable:
        try:
            result = verifier.verify(citation)
        except Exception:
            logger.exception("Verifier raised unexpectedly for citation: %r", citation.raw_text)
            citation.verification_status = "ERROR"
            citation.verification_detail = "Verification service raised an error."
            continue

        citation.verification_status = result.status
        citation.verification_detail = result.detail
        citation.candidate_cluster_ids = result.candidate_cluster_ids
        citation.candidate_metadata = result.candidate_metadata


def _verify_batched(
    verifiable: list[CitationResult],
    verifier: Any,
    batch_delay_seconds: float = 0.5,
) -> None:
    """Verify citations in batches.  Falls back to single mode per-batch on failure.

    *batch_delay_seconds* — seconds to sleep between batches when there are
    multiple batches, to avoid hitting CourtListener's rate limit.  Set to 0.0
    to disable the delay (e.g. in tests).
    """
    batches = _split_into_batches(verifiable)
    total = len(verifiable)
    completed = 0
    num_batches = len(batches)
    logger.info("Verification: %d citation(s) split into %d batch(es)", total, num_batches)

    for batch_idx, batch in enumerate(batches):
        if batch_idx > 0 and num_batches > 1 and batch_delay_seconds > 0:
            time.sleep(batch_delay_seconds)

        logger.info(
            "Verifying citations: %d/%d complete (batch %d/%d)",
            completed,
            total,
            batch_idx + 1,
            num_batches,
        )

        try:
            results = verifier.verify_batch(batch)
        except Exception:
            logger.warning(
                "Batch %d/%d failed, falling back to single-citation mode (%d citations)",
                batch_idx + 1,
                num_batches,
                len(batch),
            )
            for citation in batch:
                try:
                    result = verifier.verify(citation)
                except Exception:
                    logger.exception(
                        "Verifier raised during batch fallback for citation: %r",
                        citation.raw_text,
                    )
                    citation.verification_status = "ERROR"
                    citation.verification_detail = "Verification service raised an error."
                    continue
                citation.verification_status = result.status
                citation.verification_detail = result.detail
                citation.candidate_cluster_ids = result.candidate_cluster_ids
                citation.candidate_metadata = result.candidate_metadata
            completed += len(batch)
            continue

        for citation, result in zip(batch, results, strict=False):
            citation.verification_status = result.status
            citation.verification_detail = result.detail
            citation.candidate_cluster_ids = result.candidate_cluster_ids
            citation.candidate_metadata = result.candidate_metadata

        completed += len(batch)

    logger.info("Verification: %d/%d complete", completed, total)


def verify_citations(
    citations: list[CitationResult],
    *,
    courtlistener_token: str | None,
    verification_base_url: str,
    verifier: CitationVerifier | None = None,
    courtlistener_timeout_seconds: int = 30,
    batch_verification: bool = True,
    resolution_cache: dict[str, Any] | None = None,
    search_fallback_enabled: bool = True,
    search_url: str | None = None,
    virginia_statute_verification: bool = True,
    virginia_statute_verifier: VirginiaStatuteVerifier | None = None,
    statute_cache: dict[str, dict[str, Any]] | None = None,
    virginia_statute_timeout_seconds: int = 10,
    federal_statute_verification: bool = True,
    federal_statute_verifier: FederalStatuteVerifier | None = None,
    govinfo_api_key: str | None = None,
    federal_statute_timeout_seconds: int = 15,
    cap_fallback_enabled: bool = True,
    cap_api_key: str | None = None,
    cap_timeout_seconds: int = 15,
    cap_verifier: Any = None,
    local_index: Any = None,
    local_index_enabled: bool = True,
) -> list[CitationResult]:
    # ── First pass: handle STATUTE, DERIVED, NO_TOKEN, and cache / local-index hits ──
    # Pre-load local index hits in a single batch query to avoid N per-citation
    # round-trips to the database.
    _local_hits: dict[str, Any] = {}
    if local_index is not None and local_index_enabled:
        all_cites = [c.normalized_text or c.raw_text for c in citations]
        try:
            _local_hits = local_index.lookup_batch(all_cites)
        except Exception:
            logger.exception("Local index batch lookup failed; skipping local index for this run")

    verifiable: list[CitationResult] = []
    statute_count = 0
    derived_count = 0
    cache_count = 0
    local_index_count = 0

    for citation in citations:
        if is_statute_citation(citation):
            citation.verification_status = "STATUTE_DETECTED"
            citation.verification_detail = (
                "Statute citation detected — verification not available for this jurisdiction yet."
            )
            statute_count += 1
            continue

        if is_derived_citation(citation):
            citation.verification_status = "DERIVED"
            # Detail will be updated in the post-processing pass once all parents are verified.
            citation.verification_detail = (
                f"Derived from {citation.resolved_from or 'unknown prior citation'}."
            )
            derived_count += 1
            continue

        if is_supra_citation(citation):
            # Supra citations must never be sent to CourtListener or cached
            # under the bare key "supra," — they are pre-resolved by
            # resolve_supra_citations() in the extraction pipeline.
            if citation.resolved_from is not None:
                citation.verification_status = "DERIVED"
                citation.resolution_method = "supra_ref"
                citation.verification_detail = (
                    f"Derived from {citation.resolved_from} — supra back-reference."
                )
            else:
                antecedent = citation.antecedent_guess or "unknown"
                citation.verification_status = "AMBIGUOUS"
                citation.verification_detail = (
                    f"Supra back-reference to '{antecedent}' — "
                    "no matching earlier citation found in this document."
                )
            derived_count += 1
            continue

        if not courtlistener_token:
            citation.verification_status = "UNVERIFIED_NO_TOKEN"
            citation.verification_detail = "No CourtListener token configured."
            continue

        # Cache-first: check resolution cache before sending to CourtListener
        if resolution_cache is not None:
            cache_key = citation.normalized_text or citation.raw_text
            cached = resolution_cache.get(cache_key)
            if cached is not None:
                citation.verification_status = "VERIFIED"
                citation.selected_cluster_id = cached["cluster_id"]
                citation.resolution_method = "cache"
                case_name = cached.get("case_name") or ""
                if case_name:
                    citation.verification_detail = f"Resolved from cache. {case_name}."
                else:
                    citation.verification_detail = "Resolved from cache."
                cache_count += 1
                logger.debug(
                    "Cache hit for citation: %r → cluster %d",
                    citation.raw_text,
                    cached["cluster_id"],
                )
                continue

        # Local citation index: fast zero-network lookup from bulk data
        if _local_hits:
            cite_key = citation.normalized_text or citation.raw_text
            hit = _local_hits.get(cite_key)
            if hit is not None:
                cid = hit["cluster_id"]
                case_name = hit.get("case_name") or ""
                citation.verification_status = "VERIFIED"
                citation.selected_cluster_id = cid
                citation.resolution_method = "local_index"
                citation.candidate_cluster_ids = [cid]
                citation.candidate_metadata = [
                    {
                        "cluster_id": cid,
                        "case_name": case_name or None,
                        "court": hit.get("court_id"),
                        "date_filed": hit.get("date_filed"),
                    }
                ]
                if case_name:
                    citation.verification_detail = f"Matched in local citation index. {case_name}."
                else:
                    citation.verification_detail = "Matched in local citation index."
                local_index_count += 1
                logger.debug(
                    "Local index hit for citation: %r → cluster %d",
                    citation.raw_text,
                    cid,
                )
                continue

        verifiable.append(citation)

    if cache_count:
        logger.info("Cache resolved %d citation(s) before CourtListener", cache_count)
    if local_index_count:
        logger.info("Local index resolved %d citation(s) before CourtListener", local_index_count)
    logger.info(
        "Verification starting: %d total, %d case law, %d statute, %d derived, "
        "%d cache, %d local_index",
        len(citations),
        len(verifiable),
        statute_count,
        derived_count,
        cache_count,
        local_index_count,
    )

    # ── Virginia statute verification pass ──────────────────────────────────
    # For citations marked STATUTE_DETECTED, attempt Virginia Code verification
    # when the citation clearly references the Code of Virginia.  Non-Virginia
    # statutes are left as STATUTE_DETECTED; errors also keep STATUTE_DETECTED
    # so we never downgrade.
    if virginia_statute_verification:
        active_va_verifier = virginia_statute_verifier or VirginiaStatuteVerifier(
            timeout_seconds=virginia_statute_timeout_seconds
        )
        va_verified = 0
        va_not_found = 0
        for citation in citations:
            if citation.verification_status != "STATUTE_DETECTED":
                continue
            section_number = parse_virginia_section(citation.normalized_text or citation.raw_text)
            if section_number is None:
                continue  # not a Virginia Code citation

            # Check statute cache before hitting the API
            cached_entry = (statute_cache or {}).get(section_number)
            if cached_entry is not None:
                status = cached_entry["status"]
                section_title = cached_entry.get("section_title")
                logger.debug("Statute cache hit for %s → %s", section_number, status)
            else:
                status, section_title = active_va_verifier.verify(section_number)
                # Populate in-memory cache so later citations in the same run
                # don't repeat the lookup (the DB write is handled by the caller)
                if statute_cache is not None and status != "STATUTE_ERROR":
                    statute_cache[section_number] = {
                        "status": status,
                        "section_title": section_title,
                    }

            if status == "STATUTE_VERIFIED":
                citation.verification_status = "STATUTE_VERIFIED"
                title_suffix = f" — {section_title}" if section_title else ""
                citation.verification_detail = (
                    f"Virginia Code § {section_number} confirmed in the Code of Virginia"
                    f"{title_suffix}."
                )
                va_verified += 1
            elif status == "STATUTE_NOT_FOUND":
                # Keep STATUTE_DETECTED; update detail to reflect the lookup result
                citation.verification_detail = (
                    f"Virginia Code § {section_number} — could not be verified"
                    " against the Virginia LIS database."
                )
                va_not_found += 1
            # STATUTE_ERROR → leave verification_detail unchanged

        if va_verified or va_not_found:
            logger.info(
                "Virginia statute verification: %d verified, %d not found",
                va_verified,
                va_not_found,
            )

    # ── Federal statute verification pass ───────────────────────────────────
    # For STATUTE_DETECTED citations that look like U.S. Code references,
    # attempt verification via the GovInfo API when an API key is configured.
    # Citations that are not U.S.C., or where the API key is missing, are left
    # as STATUTE_DETECTED.  Errors also keep STATUTE_DETECTED so we never
    # downgrade a detected citation to something worse.
    effective_api_key = govinfo_api_key or ""
    if federal_statute_verification and effective_api_key:
        active_fed_verifier = federal_statute_verifier or FederalStatuteVerifier(
            api_key=effective_api_key,
            timeout_seconds=federal_statute_timeout_seconds,
        )
        fed_verified = 0
        fed_not_found = 0
        for citation in citations:
            if citation.verification_status != "STATUTE_DETECTED":
                continue
            parsed = parse_federal_section(citation.normalized_text or citation.raw_text)
            if parsed is None:
                continue  # not a U.S.C. citation

            title, section = parsed
            cache_key = f"usc:{title}-{section}"

            cached_entry = (statute_cache or {}).get(cache_key)
            if cached_entry is not None:
                status = cached_entry["status"]
                section_title = cached_entry.get("section_title")
                logger.debug("Statute cache hit for %s → %s", cache_key, status)
            else:
                status, section_title = active_fed_verifier.verify(title, section)
                if statute_cache is not None and status != "STATUTE_ERROR":
                    statute_cache[cache_key] = {
                        "status": status,
                        "section_title": section_title,
                    }

            if status == "STATUTE_VERIFIED":
                citation.verification_status = "STATUTE_VERIFIED"
                title_suffix = f" — {section_title}" if section_title else ""
                citation.verification_detail = (
                    f"{title} U.S.C. § {section} confirmed in the United States"
                    f" Code via GovInfo{title_suffix}."
                )
                fed_verified += 1
            elif status == "STATUTE_NOT_FOUND":
                citation.verification_detail = (
                    f"{title} U.S.C. § {section} — could not be verified"
                    " against the GovInfo database."
                )
                fed_not_found += 1
            # STATUTE_ERROR → leave verification_detail unchanged

        if fed_verified or fed_not_found:
            logger.info(
                "Federal statute verification: %d verified, %d not found",
                fed_verified,
                fed_not_found,
            )

    # ── Update detail for federal statutes when no API key configured ──────────
    if not effective_api_key:
        for citation in citations:
            if citation.verification_status != "STATUTE_DETECTED":
                continue
            parsed = parse_federal_section(citation.normalized_text or citation.raw_text)
            if parsed is not None:
                citation.verification_detail = (
                    "Federal statute detected — verification requires GovInfo API key"
                    " (not configured)."
                )

    if not verifiable or not courtlistener_token:
        _postprocess_citations(citations)
        return citations

    # ── Build verifier ──
    active_verifier = verifier or CourtListenerVerifier(
        token=courtlistener_token,
        base_url=verification_base_url,
        timeout_seconds=courtlistener_timeout_seconds,
    )

    # ── Second pass: verify case-law citations ──
    use_batch = batch_verification and hasattr(active_verifier, "verify_batch")

    if use_batch:
        _verify_batched(verifiable, active_verifier)
    else:
        _verify_single(verifiable, active_verifier)

    # ── Post-verification: tag direct single-cluster matches ────────────────
    # Newly-VERIFIED citations from the CourtListener 200 path have their
    # cluster info in candidate_cluster_ids (populated by map_courtlistener_result
    # above).  Set selected_cluster_id and resolution_method so these can be
    # cached and appear consistently in the history UI.
    for citation in verifiable:
        if (
            citation.verification_status == "VERIFIED"
            and citation.selected_cluster_id is None
            and citation.candidate_cluster_ids
            and len(citation.candidate_cluster_ids) == 1
        ):
            citation.selected_cluster_id = citation.candidate_cluster_ids[0]
            citation.resolution_method = "direct"

    # ── Third pass: dedup auto-resolve ──────────────────────────────────────
    # AMBIGUOUS citations reduced to a single unique candidate are resolved
    # automatically.  (CourtListener occasionally returns duplicate cluster
    # IDs for the same opinion; deduplication already happened in
    # map_courtlistener_result, so candidate_metadata is already deduped here.)
    dedup_resolved = 0
    for citation in verifiable:
        if (
            citation.verification_status == "AMBIGUOUS"
            and citation.candidate_metadata
            and len(citation.candidate_metadata) == 1
        ):
            winner = citation.candidate_metadata[0]
            citation.verification_status = "VERIFIED"
            citation.selected_cluster_id = winner["cluster_id"]
            citation.resolution_method = "dedup"
            case_name = winner.get("case_name") or ""
            if case_name:
                citation.verification_detail = (
                    f"Resolved automatically (duplicate candidates removed). {case_name}."
                )
            else:
                citation.verification_detail = (
                    "Resolved automatically (duplicate candidates removed)."
                )
            dedup_resolved += 1
            logger.info(
                "Dedup auto-resolved: %r → cluster %d",
                citation.raw_text,
                winner["cluster_id"],
            )

    if dedup_resolved:
        logger.info("Dedup auto-resolved %d citation(s)", dedup_resolved)

    # ── Fourth pass: search fallback for NOT_FOUND citations ────────────────
    if search_fallback_enabled and courtlistener_token:
        from app.services.search_fallback import (
            COURTLISTENER_SEARCH_URL,
            try_search_fallback,
        )

        effective_search_url = search_url or COURTLISTENER_SEARCH_URL
        fallback_resolved = 0
        fallback_ambiguous = 0

        for citation in verifiable:
            if citation.verification_status != "NOT_FOUND":
                continue

            fb = try_search_fallback(
                citation,
                token=courtlistener_token,
                search_url=effective_search_url,
                timeout_seconds=courtlistener_timeout_seconds,
            )
            if fb is None:
                continue

            citation.verification_status = fb.status
            citation.verification_detail = fb.detail
            citation.candidate_cluster_ids = fb.candidate_cluster_ids
            citation.candidate_metadata = fb.candidate_metadata

            if fb.status == "VERIFIED":
                citation.resolution_method = "search_fallback"
                citation.selected_cluster_id = (fb.candidate_cluster_ids or [None])[0]
                fallback_resolved += 1
            else:
                fallback_ambiguous += 1

        if fallback_resolved or fallback_ambiguous:
            logger.info(
                "Search fallback: %d resolved, %d still ambiguous",
                fallback_resolved,
                fallback_ambiguous,
            )

    # ── Fourth-B pass: CAP fallback for remaining NOT_FOUND citations ────────
    # Harvard Caselaw Access Project is tried as a secondary database when
    # CourtListener search fallback still leaves citations NOT_FOUND.
    # Gracefully degrades to a no-op when the CAP API is unavailable.
    if cap_fallback_enabled:
        from app.services.cap_verification import CAPVerifier
        from app.services.disambiguation import extract_case_name_from_text

        active_cap: Any = cap_verifier or CAPVerifier(
            api_key=cap_api_key,
            timeout_seconds=cap_timeout_seconds,
        )
        cap_resolved = 0
        cap_ambiguous = 0

        for citation in verifiable:
            if citation.verification_status != "NOT_FOUND":
                continue

            normalized = citation.normalized_text or citation.raw_text
            case_name: str | None = None
            for src in (citation.snippet, citation.raw_text):
                if src:
                    case_name = extract_case_name_from_text(src)
                    if case_name:
                        break

            fb = active_cap.verify_citation(normalized, case_name)
            if fb is None:
                continue

            citation.verification_status = fb.status
            citation.verification_detail = fb.detail
            citation.candidate_cluster_ids = fb.candidate_cluster_ids
            citation.candidate_metadata = fb.candidate_metadata

            if fb.status == "VERIFIED":
                citation.resolution_method = "cap_fallback"
                citation.selected_cluster_id = (fb.candidate_cluster_ids or [None])[0]
                cap_resolved += 1
            else:
                cap_ambiguous += 1

        if cap_resolved or cap_ambiguous:
            logger.info(
                "CAP fallback: %d resolved, %d still ambiguous",
                cap_resolved,
                cap_ambiguous,
            )

    # ── Fifth pass: heuristic auto-disambiguation of AMBIGUOUS citations ─────
    # Runs after both dedup and search fallback so it applies to AMBIGUOUS
    # results from all sources (initial verification and search fallback).
    heuristic_resolved = 0
    for citation in verifiable:
        if citation.verification_status == "AMBIGUOUS" and citation.candidate_metadata:
            winner = try_heuristic_resolution(
                citation.raw_text,
                citation.snippet,
                citation.candidate_metadata,
            )
            if winner is not None:
                citation.verification_status = "VERIFIED"
                citation.selected_cluster_id = winner["cluster_id"]
                citation.resolution_method = "heuristic"
                case_name = winner.get("case_name") or ""
                detail = f"Auto-resolved by heuristic (cluster {winner['cluster_id']})"
                if case_name:
                    detail += f". {case_name}"
                citation.verification_detail = detail + "."
                heuristic_resolved += 1
                logger.info(
                    "Heuristic resolved: %r → cluster %d (%s)",
                    citation.raw_text,
                    winner["cluster_id"],
                    case_name,
                )

    if heuristic_resolved:
        logger.info("Heuristic auto-resolved %d citation(s)", heuristic_resolved)

    # ── Sixth pass: short-citation matching ──────────────────────────────────
    # For unresolved ShortCaseCitation entries, check whether a VERIFIED full
    # citation in the same run shares the same reporter + volume.  If so,
    # resolve the short cite to the same cluster.
    #
    # Example: "588 U.S. at 392" matches VERIFIED "588 U.S. 388" because
    # both are volume 588 of the U.S. reporter.
    verified_by_vol_reporter: dict[tuple[str, str], CitationResult] = {}
    for citation in citations:
        if citation.verification_status == "VERIFIED":
            parsed = _parse_volume_reporter(citation.raw_text)
            if parsed:
                key = parsed  # (volume_str, reporter_lower)
                # Keep the first VERIFIED match per (volume, reporter)
                if key not in verified_by_vol_reporter:
                    verified_by_vol_reporter[key] = citation

    short_cite_resolved = 0
    for citation in citations:
        if citation.citation_type != "ShortCaseCitation":
            continue
        if citation.verification_status not in ("AMBIGUOUS", "NOT_FOUND"):
            continue

        parsed = _parse_volume_reporter(citation.raw_text)
        if parsed is None:
            continue

        matched_full = verified_by_vol_reporter.get(parsed)
        if matched_full is None:
            continue

        full_cite_text = matched_full.raw_text
        # Use the full citation's cluster_id; may be None for plain VERIFIED citations
        cluster_id = matched_full.selected_cluster_id
        citation.verification_status = "VERIFIED"
        citation.selected_cluster_id = cluster_id
        citation.resolution_method = "short_cite_match"
        if cluster_id is not None:
            citation.candidate_cluster_ids = [cluster_id]
        citation.verification_detail = (
            f"Matched to verified citation {full_cite_text!r} (same reporter and volume)."
        )
        short_cite_resolved += 1
        logger.info(
            "Short-cite match: %r → cluster %s via %r",
            citation.raw_text,
            cluster_id,
            full_cite_text,
        )

    if short_cite_resolved:
        logger.info("Short-cite match resolved %d citation(s)", short_cite_resolved)

    # ── Seventh pass: parallel citation resolution ────────────────────────────
    # When a NOT_FOUND citation appears immediately adjacent to a VERIFIED
    # citation in the source text (e.g. "283 Va. 474, 722 S.E.2d 272"), it
    # is likely a parallel reporter citation for the same case.
    parallel_resolved = _resolve_parallel_citations(citations)
    if parallel_resolved:
        logger.info("Parallel citation resolution: %d citation(s) resolved", parallel_resolved)

    _postprocess_citations(citations)

    summary = summarize_verification_statuses(citations)
    logger.info("Verification complete: %s", summary)

    return citations


def summarize_verification_statuses(citations: list[CitationResult]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for citation in citations:
        status = citation.verification_status or "UNKNOWN"
        summary[status] = summary.get(status, 0) + 1
    return summary
