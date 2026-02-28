from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib import error, parse, request

from app.services.audit import CitationResult


@dataclass
class VerificationResponse:
    status: str
    detail: str


class CitationVerifier(Protocol):
    def verify(self, citation: CitationResult) -> VerificationResponse: ...


def is_derived_citation(citation: CitationResult) -> bool:
    return citation.raw_text.lower().startswith("id.") or citation.citation_type.lower().startswith(
        "id"
    )


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
        return VerificationResponse(status="AMBIGUOUS", detail=detail)

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
    def __init__(self, token: str, base_url: str, timeout_seconds: int = 8) -> None:
        self.token = token
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds

    def verify(self, citation: CitationResult) -> VerificationResponse:
        lookup_text = citation.normalized_text or citation.raw_text
        if not lookup_text:
            return VerificationResponse(status="NOT_FOUND", detail="Citation text unavailable.")

        form_data = parse.urlencode({"text": lookup_text}).encode("utf-8")
        req = request.Request(
            self.base_url,
            data=form_data,
            method="POST",
            headers={
                "Authorization": f"Token {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            if exc.code == 429:
                return VerificationResponse(
                    status="ERROR",
                    detail="CourtListener rate limit reached; please retry later.",
                )
            if exc.code == 404:
                return VerificationResponse(
                    status="NOT_FOUND",
                    detail="No match found in CourtListener.",
                )
            if exc.code == 400:
                return VerificationResponse(
                    status="ERROR",
                    detail="CourtListener rejected citation lookup request.",
                )
            return VerificationResponse(
                status="ERROR", detail=f"Verification HTTP error: {exc.code}."
            )
        except (json.JSONDecodeError, UnicodeDecodeError):
            return VerificationResponse(
                status="ERROR", detail="Could not parse verification response."
            )
        except Exception:
            return VerificationResponse(status="ERROR", detail="Verification request failed.")

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


def verify_citations(
    citations: list[CitationResult],
    *,
    courtlistener_token: str | None,
    verification_base_url: str,
    verifier: CitationVerifier | None = None,
    verification_timeout_seconds: int = 8,
) -> list[CitationResult]:
    if not courtlistener_token:
        for citation in citations:
            citation.verification_status = "UNVERIFIED_NO_TOKEN"
            citation.verification_detail = "No CourtListener token configured."
        return citations

    active_verifier = verifier or CourtListenerVerifier(
        token=courtlistener_token,
        base_url=verification_base_url,
        timeout_seconds=verification_timeout_seconds,
    )

    for citation in citations:
        if is_derived_citation(citation):
            citation.verification_status = "AMBIGUOUS"
            citation.verification_detail = (
                "Derived Id. citation; not directly verified with CourtListener."
            )
            continue

        try:
            result = active_verifier.verify(citation)
        except Exception:
            citation.verification_status = "ERROR"
            citation.verification_detail = "Verification service raised an error."
            continue

        citation.verification_status = result.status
        citation.verification_detail = result.detail

    return citations


def summarize_verification_statuses(citations: list[CitationResult]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for citation in citations:
        status = citation.verification_status or "UNKNOWN"
        summary[status] = summary.get(status, 0) + 1
    return summary
