import json
import logging
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from aaa_db.models import AuditRun, CitationResolutionCache, CitationResultRecord
from app.services.audit import CitationResult

logger = logging.getLogger(__name__)

EXCERPT_LENGTH = 200


def _build_excerpt(source_type: str, input_text: str) -> str | None:
    if source_type != "text":
        return None

    cleaned = input_text.strip()
    if not cleaned:
        return None

    return cleaned[:EXCERPT_LENGTH]


def _warning_text(warnings: Sequence[str]) -> str | None:
    if not warnings:
        return None
    return "\n".join(warnings)


def save_audit_run(
    db: Session,
    *,
    source_type: str,
    source_name: str | None,
    input_text: str,
    warnings: Sequence[str],
    citations: Sequence[CitationResult],
) -> AuditRun:
    status_counts = {
        "VERIFIED": 0,
        "NOT_FOUND": 0,
        "AMBIGUOUS": 0,
        "ERROR": 0,
        "UNVERIFIED_NO_TOKEN": 0,
        "DERIVED": 0,
        "STATUTE_DETECTED": 0,
    }

    for citation in citations:
        if citation.verification_status in status_counts:
            status_counts[citation.verification_status] += 1

    audit_run = AuditRun(
        source_type=source_type,
        source_name=source_name,
        citation_count=len(citations),
        verified_count=status_counts["VERIFIED"],
        not_found_count=status_counts["NOT_FOUND"],
        ambiguous_count=status_counts["AMBIGUOUS"],
        derived_count=status_counts["DERIVED"],
        statute_count=status_counts["STATUTE_DETECTED"],
        error_count=status_counts["ERROR"],
        unverified_no_token_count=status_counts["UNVERIFIED_NO_TOKEN"],
        input_text_excerpt=_build_excerpt(source_type, input_text),
        warning_text=_warning_text(warnings),
    )

    for citation in citations:
        audit_run.citations.append(
            CitationResultRecord(
                raw_text=citation.raw_text,
                citation_type=citation.citation_type,
                normalized_text=citation.normalized_text,
                resolved_from=citation.resolved_from,
                verification_status=citation.verification_status,
                verification_detail=citation.verification_detail,
                snippet=citation.snippet,
                candidate_cluster_ids=json.dumps(citation.candidate_cluster_ids)
                if citation.candidate_cluster_ids
                else None,
                candidate_metadata=json.dumps(citation.candidate_metadata)
                if citation.candidate_metadata
                else None,
                selected_cluster_id=citation.selected_cluster_id,
                resolution_method=citation.resolution_method,
            )
        )

    db.add(audit_run)
    db.commit()
    db.refresh(audit_run)

    # Write heuristic resolutions to the resolution cache
    for citation in citations:
        if citation.resolution_method == "heuristic" and citation.selected_cluster_id is not None:
            _upsert_resolution_cache(
                db,
                normalized_cite=citation.normalized_text or citation.raw_text,
                selected_cluster_id=citation.selected_cluster_id,
                candidate_metadata=citation.candidate_metadata,
                resolution_method="heuristic",
            )
    db.commit()
    logger.info(
        "Audit run saved: id=%d, source_type=%s, citations=%d",
        audit_run.id,
        source_type,
        len(citations),
    )
    return audit_run


def _upsert_resolution_cache(
    db: Session,
    *,
    normalized_cite: str,
    selected_cluster_id: int,
    candidate_metadata: list[dict] | None,
    resolution_method: str,
) -> None:
    """Insert or update a CitationResolutionCache row (no commit)."""
    selected_meta: dict = {}
    if candidate_metadata:
        for meta in candidate_metadata:
            if meta.get("cluster_id") == selected_cluster_id:
                selected_meta = meta
                break

    case_name = selected_meta.get("case_name") or None
    court = selected_meta.get("court") or None
    date_filed = selected_meta.get("date_filed") or None

    cached = db.scalar(
        select(CitationResolutionCache).where(
            CitationResolutionCache.normalized_cite == normalized_cite
        )
    )
    if cached:
        cached.selected_cluster_id = selected_cluster_id
        cached.case_name = case_name
        cached.court = court
        cached.date_filed = date_filed
        cached.resolution_method = resolution_method
    else:
        db.add(
            CitationResolutionCache(
                normalized_cite=normalized_cite,
                selected_cluster_id=selected_cluster_id,
                case_name=case_name,
                court=court,
                date_filed=date_filed,
                resolution_method=resolution_method,
            )
        )


def list_audit_runs(db: Session) -> list[AuditRun]:
    stmt = select(AuditRun).order_by(AuditRun.created_at.desc(), AuditRun.id.desc())
    return list(db.scalars(stmt).all())


def get_audit_run(db: Session, run_id: int) -> AuditRun | None:
    stmt = select(AuditRun).options(selectinload(AuditRun.citations)).where(AuditRun.id == run_id)
    return db.scalar(stmt)


def get_citation(db: Session, citation_id: int) -> CitationResultRecord | None:
    return db.get(CitationResultRecord, citation_id)


def resolve_citation(
    db: Session,
    citation: CitationResultRecord,
    *,
    selected_cluster_id: int,
    resolution_method: str,
    candidate_metadata: list[dict] | None,
) -> CitationResultRecord:
    """Mark a citation as user-resolved and update the resolution cache."""
    citation.selected_cluster_id = selected_cluster_id
    citation.resolution_method = resolution_method
    citation.verification_status = "VERIFIED"

    # Find matching cluster metadata for detail text
    selected_meta: dict = {}
    if candidate_metadata:
        for meta in candidate_metadata:
            if meta.get("cluster_id") == selected_cluster_id:
                selected_meta = meta
                break

    case_name = selected_meta.get("case_name") or ""
    detail_parts = [f"Resolved by user (cluster {selected_cluster_id})"]
    if case_name:
        detail_parts.append(case_name)
    citation.verification_detail = ". ".join(detail_parts) + "."

    # Upsert into resolution cache
    _upsert_resolution_cache(
        db,
        normalized_cite=citation.normalized_text or citation.raw_text,
        selected_cluster_id=selected_cluster_id,
        candidate_metadata=candidate_metadata,
        resolution_method=resolution_method,
    )

    db.commit()
    db.refresh(citation)

    # Recalculate run summary counts from current citation statuses
    all_citations = db.scalars(
        select(CitationResultRecord).where(
            CitationResultRecord.audit_run_id == citation.audit_run_id
        )
    ).all()
    run = db.get(AuditRun, citation.audit_run_id)
    if run is not None:
        status_counts = {
            "VERIFIED": 0,
            "NOT_FOUND": 0,
            "AMBIGUOUS": 0,
            "ERROR": 0,
            "UNVERIFIED_NO_TOKEN": 0,
            "DERIVED": 0,
            "STATUTE_DETECTED": 0,
        }
        for c in all_citations:
            if c.verification_status in status_counts:
                status_counts[c.verification_status] += 1
        run.verified_count = status_counts["VERIFIED"]
        run.not_found_count = status_counts["NOT_FOUND"]
        run.ambiguous_count = status_counts["AMBIGUOUS"]
        run.error_count = status_counts["ERROR"]
        run.unverified_no_token_count = status_counts["UNVERIFIED_NO_TOKEN"]
        run.derived_count = status_counts["DERIVED"]
        run.statute_count = status_counts["STATUTE_DETECTED"]
        db.commit()

    logger.info(
        "Citation resolved: id=%d, cluster_id=%d, method=%s",
        citation.id,
        selected_cluster_id,
        resolution_method,
    )
    return citation
