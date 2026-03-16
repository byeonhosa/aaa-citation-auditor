import json
import logging
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from aaa_db.models import (
    AuditRun,
    CitationResolutionCache,
    CitationResultRecord,
    StatuteVerificationCache,
)
from app.services.audit import CitationResult

logger = logging.getLogger(__name__)

# Resolution-method confidence levels (higher = more trustworthy).
# When a cache entry already exists, only update it if the new resolution has
# equal or higher confidence — never downgrade a high-quality entry.
_RESOLUTION_CONFIDENCE: dict[str, int] = {
    "user": 5,
    "heuristic": 4,
    "direct": 3,
    "dedup": 3,
    "search_fallback": 2,
    "cap_fallback": 2,
    "local_index": 3,
    "short_cite_match": 1,
    "cache": 0,  # never re-cache a cache hit
}

# Methods that are eligible to be written into the cache by save_audit_run.
# "user" is excluded here because it is cached immediately by resolve_citation().
# "cache" is excluded because it is already a cache hit.
_CACHEABLE_METHODS = frozenset(
    {
        "direct",
        "heuristic",
        "dedup",
        "search_fallback",
        "cap_fallback",
        "local_index",
        "short_cite_match",
    }
)

_TRUST_TIER_MAP: dict[str, str] = {
    "direct": "authoritative",
    "local_index": "authoritative",
    "heuristic": "algorithmic",
    "dedup": "algorithmic",
    "short_cite_match": "algorithmic",
    "search_fallback": "algorithmic",
    "cap_fallback": "algorithmic",
    "user": "user_submitted",
    "cache": "algorithmic",
}

_TIER_RANK: dict[str, int] = {
    "authoritative": 3,
    "algorithmic": 2,
    "user_submitted": 1,
}

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
    user_id: int | None = None,
) -> AuditRun:
    status_counts = {
        "VERIFIED": 0,
        "NOT_FOUND": 0,
        "AMBIGUOUS": 0,
        "ERROR": 0,
        "UNVERIFIED_NO_TOKEN": 0,
        "DERIVED": 0,
        "STATUTE_DETECTED": 0,
        "STATUTE_VERIFIED": 0,
    }

    for citation in citations:
        if citation.verification_status in status_counts:
            status_counts[citation.verification_status] += 1

    audit_run = AuditRun(
        user_id=user_id,
        source_type=source_type,
        source_name=source_name,
        citation_count=len(citations),
        verified_count=status_counts["VERIFIED"],
        not_found_count=status_counts["NOT_FOUND"],
        ambiguous_count=status_counts["AMBIGUOUS"],
        derived_count=status_counts["DERIVED"],
        statute_count=status_counts["STATUTE_DETECTED"],
        statute_verified_count=status_counts["STATUTE_VERIFIED"],
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

    # Write all successful verifications to the resolution cache.
    # This covers direct CourtListener matches, dedup, heuristic, search fallback,
    # and short-cite matches — so any citation verified once is never re-queried.
    #
    # `seen_cites` guards against duplicate normalized_cite values within the
    # same batch (e.g. a PDF that cites the same case ten times): the SELECT in
    # _upsert_resolution_cache runs before any db.flush(), so all duplicates
    # would appear "not found" and all queue an INSERT — crashing on commit with
    # a UNIQUE constraint violation.  We only call the upsert once per key.
    cached_count = 0
    seen_cites: set[str] = set()
    for citation in citations:
        if (
            citation.verification_status == "VERIFIED"
            and citation.selected_cluster_id is not None
            and citation.resolution_method in _CACHEABLE_METHODS
        ):
            key = citation.normalized_text or citation.raw_text
            if key in seen_cites:
                continue
            seen_cites.add(key)
            _upsert_resolution_cache(
                db,
                normalized_cite=key,
                selected_cluster_id=citation.selected_cluster_id,
                candidate_metadata=citation.candidate_metadata,
                resolution_method=citation.resolution_method,
                user_id=user_id,
            )
            cached_count += 1
    if cached_count:
        logger.info("Resolution cache: wrote %d new/updated entr(ies)", cached_count)
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
    user_id: int | None = None,
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

    new_tier = _TRUST_TIER_MAP.get(resolution_method, "algorithmic")

    cached = db.scalar(
        select(CitationResolutionCache).where(
            CitationResolutionCache.normalized_cite == normalized_cite
        )
    )
    if cached:
        existing_tier = cached.trust_tier or "algorithmic"

        # Never overwrite authoritative entries with user-submitted ones
        if existing_tier == "authoritative" and new_tier == "user_submitted":
            logger.debug(
                "Cache: skipping user_submitted update for %r — existing tier is authoritative",
                normalized_cite,
            )
            return

        # Only update if the new resolution is at least as confident as the
        # existing one — never downgrade a user selection to a heuristic hit.
        existing_conf = _RESOLUTION_CONFIDENCE.get(cached.resolution_method, 0)
        new_conf = _RESOLUTION_CONFIDENCE.get(resolution_method, 0)
        if new_conf < existing_conf:
            logger.debug(
                "Cache: skipping update for %r — existing method %r (%d) outranks %r (%d)",
                normalized_cite,
                cached.resolution_method,
                existing_conf,
                resolution_method,
                new_conf,
            )
            return

        cached.selected_cluster_id = selected_cluster_id
        cached.case_name = case_name
        cached.court = court
        cached.date_filed = date_filed
        cached.resolution_method = resolution_method

        # Upgrade trust tier if new tier has higher or equal rank
        new_tier_rank = _TIER_RANK.get(new_tier, 0)
        existing_tier_rank = _TIER_RANK.get(existing_tier, 0)
        if new_tier_rank >= existing_tier_rank:
            cached.trust_tier = new_tier

        # Track unique user contributions for user_submitted entries
        if (
            user_id is not None
            and cached.selected_cluster_id == selected_cluster_id
            and cached.cache_user_id != user_id
        ):
            cached.unique_user_count = (cached.unique_user_count or 1) + 1
            # Upgrade user_submitted to algorithmic after 3 unique users agree
            if cached.unique_user_count >= 3 and cached.trust_tier == "user_submitted":
                cached.trust_tier = "algorithmic"
                logger.info(
                    "Cache: upgraded %r to algorithmic (3 unique users confirmed)",
                    normalized_cite,
                )
    else:
        new_entry = CitationResolutionCache(
            normalized_cite=normalized_cite,
            selected_cluster_id=selected_cluster_id,
            case_name=case_name,
            court=court,
            date_filed=date_filed,
            resolution_method=resolution_method,
            trust_tier=new_tier,
            unique_user_count=1,
        )
        if new_tier == "user_submitted" and user_id is not None:
            new_entry.cache_user_id = user_id
        db.add(new_entry)


def lookup_resolution_cache(
    db: Session, current_user_id: int | None = None
) -> dict[str, Any]:
    """Return cache entries as a dict keyed by normalized_cite.

    Each value is a plain dict with keys: cluster_id, case_name, court,
    date_filed, resolution_method, trust_tier, cache_user_id.

    user_submitted entries from other users are excluded from automatic
    application (they appear only as suggestions via get_cache_suggestions).
    """
    rows = db.scalars(select(CitationResolutionCache)).all()
    result: dict[str, Any] = {}
    for row in rows:
        # Exclude other users' user_submitted entries from auto-apply
        if (
            row.trust_tier == "user_submitted"
            and current_user_id is not None
            and row.cache_user_id != current_user_id
        ):
            continue
        result[row.normalized_cite] = {
            "cluster_id": row.selected_cluster_id,
            "case_name": row.case_name,
            "court": row.court,
            "date_filed": row.date_filed,
            "resolution_method": row.resolution_method,
            "trust_tier": row.trust_tier,
            "cache_user_id": row.cache_user_id,
        }
    return result


def get_cache_suggestions(
    db: Session, normalized_cites: list[str], current_user_id: int
) -> dict[str, Any]:
    """Return user_submitted cache entries from other users for the given cites.

    Used to display suggestions to the current user without automatically
    applying another user's resolution.
    """
    rows = db.scalars(
        select(CitationResolutionCache).where(
            CitationResolutionCache.normalized_cite.in_(normalized_cites),
            CitationResolutionCache.trust_tier == "user_submitted",
            CitationResolutionCache.cache_user_id != current_user_id,
        )
    ).all()
    return {
        row.normalized_cite: {
            "cluster_id": row.selected_cluster_id,
            "case_name": row.case_name,
            "court": row.court,
            "date_filed": row.date_filed,
            "resolution_method": row.resolution_method,
            "trust_tier": row.trust_tier,
        }
        for row in rows
    }


def clear_cache_entry(db: Session, normalized_cite: str) -> bool:
    """Delete a user_submitted cache entry. Returns True if deleted.

    Protected (non-user_submitted) entries are not deleted; returns False.
    """
    entry = db.scalar(
        select(CitationResolutionCache).where(
            CitationResolutionCache.normalized_cite == normalized_cite
        )
    )
    if entry is None:
        return False
    if entry.trust_tier != "user_submitted":
        logger.debug(
            "Cache: refusing to clear %r (tier=%s)", normalized_cite, entry.trust_tier
        )
        return False
    db.delete(entry)
    db.commit()
    return True


def upgrade_cache_entry_trust(
    db: Session, normalized_cite: str, new_tier: str
) -> bool:
    """Upgrade a cache entry's trust_tier if new_tier has higher rank.

    Returns True if the entry was upgraded.
    """
    entry = db.scalar(
        select(CitationResolutionCache).where(
            CitationResolutionCache.normalized_cite == normalized_cite
        )
    )
    if entry is None:
        return False
    existing_rank = _TIER_RANK.get(entry.trust_tier or "algorithmic", 0)
    new_rank = _TIER_RANK.get(new_tier, 0)
    if new_rank > existing_rank:
        entry.trust_tier = new_tier
        db.commit()
        logger.info(
            "Cache: upgraded %r from %r to %r",
            normalized_cite,
            entry.trust_tier,
            new_tier,
        )
        return True
    return False


def clear_resolution_cache(db: Session) -> int:
    """Delete all entries from CitationResolutionCache. Returns the count removed."""
    count = db.query(CitationResolutionCache).count()
    db.query(CitationResolutionCache).delete()
    db.commit()
    logger.info("Resolution cache cleared: %d entries removed", count)
    return count


def save_memo_for_run(db: Session, run_id: int, memo_json: str) -> None:
    """Persist the serialized AI memo JSON for a run (overwrites any existing value)."""
    run = db.get(AuditRun, run_id)
    if run is not None:
        run.memo_json = memo_json
        db.commit()
        logger.debug("AI memo persisted for run id=%d", run_id)


def list_audit_runs(db: Session, user_id: int | None = None) -> list[AuditRun]:
    stmt = select(AuditRun).order_by(AuditRun.created_at.desc(), AuditRun.id.desc())
    if user_id is not None:
        stmt = stmt.where(AuditRun.user_id == user_id)
    return list(db.scalars(stmt).all())


def get_audit_run(
    db: Session, run_id: int, user_id: int | None = None
) -> AuditRun | None:
    stmt = select(AuditRun).options(selectinload(AuditRun.citations)).where(AuditRun.id == run_id)
    run = db.scalar(stmt)
    if run is None:
        return None
    # Ownership check: runs with no owner are hidden from all users
    if user_id is not None and run.user_id != user_id:
        return None
    return run


def get_citation(db: Session, citation_id: int) -> CitationResultRecord | None:
    return db.get(CitationResultRecord, citation_id)


def resolve_citation(
    db: Session,
    citation: CitationResultRecord,
    *,
    selected_cluster_id: int,
    resolution_method: str,
    candidate_metadata: list[dict] | None,
    user_id: int | None = None,
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
        user_id=user_id,
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
            "STATUTE_VERIFIED": 0,
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
        run.statute_verified_count = status_counts["STATUTE_VERIFIED"]
        db.commit()

    logger.info(
        "Citation resolved: id=%d, cluster_id=%d, method=%s",
        citation.id,
        selected_cluster_id,
        resolution_method,
    )
    return citation


def get_cache_stats(db: Session) -> dict[str, Any]:
    """Return cache statistics for the settings page.

    Returns a dict with:
        total           – total number of cached resolutions
        recent_hits     – cache hits in the most recent audit run
        recent_verifiable – case-law citations attempted in the most recent run
        trust_tiers     – breakdown by trust tier
    """
    total = db.query(CitationResolutionCache).count()

    # Most recent audit run
    recent_run_id = db.scalar(
        select(AuditRun.id).order_by(AuditRun.created_at.desc(), AuditRun.id.desc()).limit(1)
    )

    recent_hits = 0
    recent_verifiable = 0
    if recent_run_id is not None:
        _skip = ("STATUTE_DETECTED", "STATUTE_VERIFIED", "DERIVED", "UNVERIFIED_NO_TOKEN")
        recent_verifiable = (
            db.query(CitationResultRecord)
            .filter(
                CitationResultRecord.audit_run_id == recent_run_id,
                CitationResultRecord.verification_status.notin_(_skip),
            )
            .count()
        )
        recent_hits = (
            db.query(CitationResultRecord)
            .filter(
                CitationResultRecord.audit_run_id == recent_run_id,
                CitationResultRecord.resolution_method == "cache",
            )
            .count()
        )

    trust_tiers = {
        "authoritative": db.query(CitationResolutionCache)
        .filter(CitationResolutionCache.trust_tier == "authoritative")
        .count(),
        "algorithmic": db.query(CitationResolutionCache)
        .filter(CitationResolutionCache.trust_tier == "algorithmic")
        .count(),
        "user_submitted": db.query(CitationResolutionCache)
        .filter(CitationResolutionCache.trust_tier == "user_submitted")
        .count(),
        "disputed": db.query(CitationResolutionCache)
        .filter(CitationResolutionCache.disputed == True)  # noqa: E712
        .count(),
    }

    return {
        "total": total,
        "recent_hits": recent_hits,
        "recent_verifiable": recent_verifiable,
        "trust_tiers": trust_tiers,
    }


# ── Statute verification cache ────────────────────────────────────────────────


def lookup_statute_cache(db: Session) -> dict[str, dict[str, Any]]:
    """Return all statute cache entries as a dict keyed by section_number.

    Each value is ``{"status": ..., "section_title": ...}``.
    """
    rows = db.scalars(select(StatuteVerificationCache)).all()
    return {
        row.section_number: {
            "status": row.status,
            "section_title": row.section_title,
        }
        for row in rows
    }


def save_statute_cache_entry(
    db: Session,
    *,
    section_number: str,
    status: str,
    section_title: str | None,
) -> None:
    """Insert or update a StatuteVerificationCache row."""
    cached = db.scalar(
        select(StatuteVerificationCache).where(
            StatuteVerificationCache.section_number == section_number
        )
    )
    if cached:
        cached.status = status
        cached.section_title = section_title
    else:
        db.add(
            StatuteVerificationCache(
                section_number=section_number,
                status=status,
                section_title=section_title,
            )
        )
    db.commit()
