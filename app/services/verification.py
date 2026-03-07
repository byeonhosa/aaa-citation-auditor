from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.services.audit import CitationResult
from app.services.disambiguation import try_heuristic_resolution
from app.services.http_client import post_with_retry

logger = logging.getLogger(__name__)


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
        detail = (
            f"CourtListener matched citation (clusters: {cluster_count})."
            if cluster_count
            else "CourtListener matched citation."
        )
        return VerificationResponse(status="VERIFIED", detail=detail)

    if status_code == 404:
        detail = str(error_message).strip() if error_message else "No match found in CourtListener."
        return VerificationResponse(status="NOT_FOUND", detail=detail)

    if status_code == 300:
        detail = (
            str(error_message).strip()
            if error_message
            else "Multiple possible CourtListener matches."
        )
        candidate_cluster_ids: list[int] = []
        candidate_metadata: list[dict] = []
        for cluster in clusters:
            if not isinstance(cluster, dict):
                continue
            cid = cluster.get("id")
            if isinstance(cid, int):
                candidate_cluster_ids.append(cid)
                candidate_metadata.append(
                    {
                        "cluster_id": cid,
                        "case_name": cluster.get("case_name")
                        or cluster.get("case_name_short")
                        or "",
                        "court": cluster.get("court_id") or "",
                        "date_filed": cluster.get("date_filed") or "",
                    }
                )
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
            status="ERROR",
            detail="CourtListener rate limit reached; please retry later.",
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
                status="ERROR",
                detail="CourtListener rate limit reached; please retry later.",
            )
        if response.status_code == 404:
            return VerificationResponse(
                status="NOT_FOUND",
                detail="No match found in CourtListener.",
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
            return [
                VerificationResponse(
                    status="ERROR",
                    detail="CourtListener rate limit reached; please retry later.",
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
) -> None:
    """Verify citations in batches.  Falls back to single mode per-batch on failure."""
    batches = _split_into_batches(verifiable)
    logger.debug(
        "Verification: %d citation(s) split into %d batch(es)", len(verifiable), len(batches)
    )

    for batch_idx, batch in enumerate(batches):
        try:
            results = verifier.verify_batch(batch)
        except Exception:
            logger.warning(
                "Batch %d/%d failed, falling back to single-citation mode (%d citations)",
                batch_idx + 1,
                len(batches),
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
            continue

        for citation, result in zip(batch, results, strict=False):
            citation.verification_status = result.status
            citation.verification_detail = result.detail
            citation.candidate_cluster_ids = result.candidate_cluster_ids
            citation.candidate_metadata = result.candidate_metadata


def verify_citations(
    citations: list[CitationResult],
    *,
    courtlistener_token: str | None,
    verification_base_url: str,
    verifier: CitationVerifier | None = None,
    courtlistener_timeout_seconds: int = 30,
    batch_verification: bool = True,
) -> list[CitationResult]:
    # ── First pass: handle STATUTE, DERIVED, and NO_TOKEN ──
    verifiable: list[CitationResult] = []
    statute_count = 0
    derived_count = 0

    for citation in citations:
        if is_statute_citation(citation):
            citation.verification_status = "STATUTE_DETECTED"
            citation.verification_detail = (
                "Statute citation detected — not verified (case law verification only)."
            )
            statute_count += 1
            continue

        if is_derived_citation(citation):
            citation.verification_status = "DERIVED"
            parent = citation.resolved_from or "unknown prior citation"
            citation.verification_detail = (
                f"Derived from prior citation ({parent}); not independently verified."
            )
            derived_count += 1
            continue

        if not courtlistener_token:
            citation.verification_status = "UNVERIFIED_NO_TOKEN"
            citation.verification_detail = "No CourtListener token configured."
            continue

        verifiable.append(citation)

    logger.info(
        "Verification starting: %d total, %d case law, %d statute, %d derived",
        len(citations),
        len(verifiable),
        statute_count,
        derived_count,
    )

    if not verifiable or not courtlistener_token:
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

    # ── Third pass: heuristic auto-disambiguation of AMBIGUOUS citations ──
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

    summary = summarize_verification_statuses(citations)
    logger.info("Verification complete: %s", summary)

    return citations


def summarize_verification_statuses(citations: list[CitationResult]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for citation in citations:
        status = citation.verification_status or "UNKNOWN"
        summary[status] = summary.get(status, 0) + 1
    return summary
