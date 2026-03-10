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
    resolution_cache: dict[str, Any] | None = None,
    search_fallback_enabled: bool = True,
    search_url: str | None = None,
) -> list[CitationResult]:
    # ── First pass: handle STATUTE, DERIVED, NO_TOKEN, and cache hits ──
    verifiable: list[CitationResult] = []
    statute_count = 0
    derived_count = 0
    cache_count = 0

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

        # Cache-first: check resolution cache before sending to CourtListener
        if resolution_cache is not None:
            cache_key = citation.normalized_text or citation.raw_text
            cached = resolution_cache.get(cache_key)
            if cached is not None:
                citation.verification_status = "VERIFIED"
                citation.selected_cluster_id = cached["cluster_id"]
                citation.resolution_method = "cache"
                case_name = cached.get("case_name") or ""
                detail = f"Resolved from cache (cluster {cached['cluster_id']})"
                if case_name:
                    detail += f". {case_name}"
                citation.verification_detail = detail + "."
                cache_count += 1
                logger.debug(
                    "Cache hit for citation: %r → cluster %d",
                    citation.raw_text,
                    cached["cluster_id"],
                )
                continue

        verifiable.append(citation)

    if cache_count:
        logger.info("Cache resolved %d citation(s) before CourtListener", cache_count)
    logger.info(
        "Verification starting: %d total, %d case law, %d statute, %d derived, %d cache",
        len(citations),
        len(verifiable),
        statute_count,
        derived_count,
        cache_count,
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
            cid = winner["cluster_id"]
            detail = f"Resolved automatically (duplicate candidates removed, cluster {cid})"
            if case_name:
                detail += f". {case_name}"
            citation.verification_detail = detail + "."
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

    summary = summarize_verification_statuses(citations)
    logger.info("Verification complete: %s", summary)

    return citations


def summarize_verification_statuses(citations: list[CitationResult]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for citation in citations:
        status = citation.verification_status or "UNKNOWN"
        summary[status] = summary.get(status, 0) + 1
    return summary
