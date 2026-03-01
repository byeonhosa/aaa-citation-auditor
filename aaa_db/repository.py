from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from aaa_db.models import AuditRun, CitationResultRecord
from app.services.audit import CitationResult

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
        ambiguous_count=status_counts["AMBIGUOUS"] + status_counts["DERIVED"],
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
            )
        )

    db.add(audit_run)
    db.commit()
    db.refresh(audit_run)
    return audit_run


def list_audit_runs(db: Session) -> list[AuditRun]:
    stmt = select(AuditRun).order_by(AuditRun.created_at.desc(), AuditRun.id.desc())
    return list(db.scalars(stmt).all())


def get_audit_run(db: Session, run_id: int) -> AuditRun | None:
    stmt = select(AuditRun).options(selectinload(AuditRun.citations)).where(AuditRun.id == run_id)
    return db.scalar(stmt)
