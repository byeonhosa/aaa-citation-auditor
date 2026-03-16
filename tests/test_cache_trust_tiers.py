"""Comprehensive tests for cache trust tiers feature."""

from __future__ import annotations

import csv
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from aaa_db.models import Base, CitationResolutionCache, LocalCitationIndex
from aaa_db.repository import (
    _upsert_resolution_cache,
    clear_cache_entry,
    get_cache_stats,
    get_cache_suggestions,
    lookup_resolution_cache,
)
from app.services.local_index import import_incremental
from app.services.reverification import reverify_citation

# ── In-memory SQLite fixture ──────────────────────────────────────────────────


@pytest.fixture()
def db():
    """Isolated in-memory SQLite session with the full schema."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    engine.dispose()


# ── Helper ────────────────────────────────────────────────────────────────────


def _add_cache_entry(
    db,
    normalized_cite: str,
    cluster_id: int = 1001,
    resolution_method: str = "direct",
    trust_tier: str | None = None,
    cache_user_id: int | None = None,
    disputed: bool = False,
    unique_user_count: int = 1,
) -> CitationResolutionCache:
    tier = trust_tier or (
        "authoritative"
        if resolution_method in ("direct", "local_index")
        else "user_submitted"
        if resolution_method == "user"
        else "algorithmic"
    )
    entry = CitationResolutionCache(
        normalized_cite=normalized_cite,
        selected_cluster_id=cluster_id,
        resolution_method=resolution_method,
        trust_tier=tier,
        cache_user_id=cache_user_id,
        disputed=disputed,
        unique_user_count=unique_user_count,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def _write_csv(tmp_path: Path, filename: str, rows: list[dict]) -> Path:
    fp = tmp_path / filename
    with open(fp, "w", newline="", encoding="utf-8") as fh:
        if rows:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    return fp


# ── 1. trust_tier_assignment_authoritative ────────────────────────────────────


def test_trust_tier_assignment_authoritative(db):
    _upsert_resolution_cache(
        db,
        normalized_cite="347 U.S. 483",
        selected_cluster_id=1001,
        candidate_metadata=None,
        resolution_method="direct",
    )
    db.commit()
    entry = db.query(CitationResolutionCache).filter_by(normalized_cite="347 U.S. 483").one()
    assert entry.trust_tier == "authoritative"

    _upsert_resolution_cache(
        db,
        normalized_cite="400 F.2d 200",
        selected_cluster_id=1002,
        candidate_metadata=None,
        resolution_method="local_index",
    )
    db.commit()
    entry2 = db.query(CitationResolutionCache).filter_by(normalized_cite="400 F.2d 200").one()
    assert entry2.trust_tier == "authoritative"


# ── 2. trust_tier_assignment_algorithmic ─────────────────────────────────────


def test_trust_tier_assignment_algorithmic(db):
    for method in ("heuristic", "dedup", "search_fallback"):
        cite = f"100 F.3d {method[:3]}"
        _upsert_resolution_cache(
            db,
            normalized_cite=cite,
            selected_cluster_id=2000,
            candidate_metadata=None,
            resolution_method=method,
        )
        db.commit()
        entry = db.query(CitationResolutionCache).filter_by(normalized_cite=cite).one()
        assert entry.trust_tier == "algorithmic", f"{method!r} should be algorithmic"


# ── 3. trust_tier_assignment_user_submitted ───────────────────────────────────


def test_trust_tier_assignment_user_submitted(db):
    _upsert_resolution_cache(
        db,
        normalized_cite="100 U.S. 200",
        selected_cluster_id=3001,
        candidate_metadata=[{"cluster_id": 3001, "case_name": "Test v. Case"}],
        resolution_method="user",
        user_id=42,
    )
    db.commit()
    entry = db.query(CitationResolutionCache).filter_by(normalized_cite="100 U.S. 200").one()
    assert entry.trust_tier == "user_submitted"
    assert entry.cache_user_id == 42


# ── 4. authoritative_not_overwritten_by_user_submitted ────────────────────────


def test_authoritative_not_overwritten_by_user_submitted(db):
    # Insert authoritative entry first
    _upsert_resolution_cache(
        db,
        normalized_cite="200 U.S. 1",
        selected_cluster_id=4001,
        candidate_metadata=None,
        resolution_method="direct",
    )
    db.commit()
    entry = db.query(CitationResolutionCache).filter_by(normalized_cite="200 U.S. 1").one()
    assert entry.trust_tier == "authoritative"

    # Try to overwrite with user resolution pointing to a DIFFERENT cluster
    _upsert_resolution_cache(
        db,
        normalized_cite="200 U.S. 1",
        selected_cluster_id=9999,
        candidate_metadata=None,
        resolution_method="user",
        user_id=7,
    )
    db.commit()
    db.expire(entry)
    db.refresh(entry)
    # authoritative should be preserved; user_submitted should be skipped
    assert entry.trust_tier == "authoritative"
    assert entry.selected_cluster_id == 4001


# ── 5. user_submitted_upgrade_to_algorithmic_at_3_users ───────────────────────


def test_user_submitted_upgrade_to_algorithmic_at_3_users(db):
    # First user creates user_submitted entry
    _upsert_resolution_cache(
        db,
        normalized_cite="300 F.3d 100",
        selected_cluster_id=5001,
        candidate_metadata=None,
        resolution_method="user",
        user_id=1,
    )
    db.commit()
    entry = db.query(CitationResolutionCache).filter_by(normalized_cite="300 F.3d 100").one()
    assert entry.trust_tier == "user_submitted"
    assert entry.unique_user_count == 1

    # Second user agrees (same cluster_id) — still user_submitted
    _upsert_resolution_cache(
        db,
        normalized_cite="300 F.3d 100",
        selected_cluster_id=5001,
        candidate_metadata=None,
        resolution_method="user",
        user_id=2,
    )
    db.commit()
    db.expire(entry)
    db.refresh(entry)
    assert entry.trust_tier == "user_submitted"
    assert entry.unique_user_count == 2

    # Third user agrees — should upgrade to algorithmic
    _upsert_resolution_cache(
        db,
        normalized_cite="300 F.3d 100",
        selected_cluster_id=5001,
        candidate_metadata=None,
        resolution_method="user",
        user_id=3,
    )
    db.commit()
    db.expire(entry)
    db.refresh(entry)
    assert entry.trust_tier == "algorithmic"
    assert entry.unique_user_count == 3


# ── 6. lookup_excludes_other_users_submissions ────────────────────────────────


def test_lookup_excludes_other_users_submissions(db):
    # User 10's submission
    _add_cache_entry(
        db,
        normalized_cite="500 F.2d 10",
        cluster_id=6001,
        resolution_method="user",
        trust_tier="user_submitted",
        cache_user_id=10,
    )
    # Algorithmic entry (should always be returned)
    _add_cache_entry(
        db,
        normalized_cite="501 F.2d 11",
        cluster_id=6002,
        resolution_method="heuristic",
        trust_tier="algorithmic",
    )

    # Looking up as user 99 — user 10's entry should be excluded
    result = lookup_resolution_cache(db, current_user_id=99)
    assert "500 F.2d 10" not in result
    assert "501 F.2d 11" in result

    # Looking up as user 10 — their own entry is included
    result2 = lookup_resolution_cache(db, current_user_id=10)
    assert "500 F.2d 10" in result2
    assert result2["500 F.2d 10"]["trust_tier"] == "user_submitted"

    # Looking up with no user — user_submitted entries with a cache_user_id
    # are excluded when current_user_id is None only if filtering is active.
    # Per implementation: filtering only occurs when current_user_id is not None.
    result3 = lookup_resolution_cache(db, current_user_id=None)
    # With None user_id, user_submitted entries are included (no filter applied)
    assert "500 F.2d 10" in result3


# ── 7. clear_cache_entry_user_submitted ───────────────────────────────────────


def test_clear_cache_entry_user_submitted(db):
    _add_cache_entry(
        db,
        normalized_cite="700 F.2d 7",
        cluster_id=7001,
        resolution_method="user",
        trust_tier="user_submitted",
    )
    result = clear_cache_entry(db, "700 F.2d 7")
    assert result is True
    entry = db.query(CitationResolutionCache).filter_by(normalized_cite="700 F.2d 7").first()
    assert entry is None


# ── 8. clear_cache_entry_authoritative_protected ─────────────────────────────


def test_clear_cache_entry_authoritative_protected(db):
    _add_cache_entry(
        db,
        normalized_cite="800 U.S. 8",
        cluster_id=8001,
        resolution_method="direct",
        trust_tier="authoritative",
    )
    result = clear_cache_entry(db, "800 U.S. 8")
    assert result is False
    entry = db.query(CitationResolutionCache).filter_by(normalized_cite="800 U.S. 8").first()
    assert entry is not None


# ── 9. cache_stats_include_tier_breakdown ─────────────────────────────────────


def test_cache_stats_include_tier_breakdown(db):
    _add_cache_entry(
        db, "900 U.S. 1", cluster_id=9001,
        resolution_method="direct", trust_tier="authoritative",
    )
    _add_cache_entry(
        db, "901 F.2d 1", cluster_id=9002,
        resolution_method="heuristic", trust_tier="algorithmic",
    )
    _add_cache_entry(
        db, "902 F.3d 1", cluster_id=9003, resolution_method="user",
        trust_tier="user_submitted", cache_user_id=5,
    )
    _add_cache_entry(
        db, "903 F.3d 2", cluster_id=9004, resolution_method="user",
        trust_tier="user_submitted", cache_user_id=6, disputed=True,
    )

    stats = get_cache_stats(db)
    assert "trust_tiers" in stats
    tiers = stats["trust_tiers"]
    assert tiers["authoritative"] == 1
    assert tiers["algorithmic"] == 1
    assert tiers["user_submitted"] == 2
    assert tiers["disputed"] == 1
    assert stats["total"] == 4


# ── 10. reverification_confirms_correct_resolution ───────────────────────────


def test_reverification_confirms_correct_resolution(db):
    entry = _add_cache_entry(
        db,
        normalized_cite="1001 F.2d 100",
        cluster_id=1001,
        resolution_method="user",
        trust_tier="user_submitted",
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [{"id": 1001, "citations": []}]

    mock_client = MagicMock()
    mock_client.get.return_value = mock_resp

    outcome = reverify_citation(db, entry, _client=mock_client)
    assert outcome == "confirmed"
    db.expire(entry)
    db.refresh(entry)
    assert entry.trust_tier == "authoritative"
    assert entry.disputed is False


# ── 11. reverification_flags_disputed ────────────────────────────────────────


def test_reverification_flags_disputed(db):
    entry = _add_cache_entry(
        db,
        normalized_cite="1002 F.2d 200",
        cluster_id=1002,
        resolution_method="user",
        trust_tier="user_submitted",
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    # CourtListener returns a DIFFERENT cluster id
    mock_resp.json.return_value = [{"id": 9999, "citations": []}]

    mock_client = MagicMock()
    mock_client.get.return_value = mock_resp

    outcome = reverify_citation(db, entry, _client=mock_client)
    assert outcome == "disputed"
    db.expire(entry)
    db.refresh(entry)
    assert entry.disputed is True


# ── 12. smart_update_new_insertion ────────────────────────────────────────────


def test_smart_update_new_insertion(db, tmp_path):
    csv_file = _write_csv(
        tmp_path,
        "citations.csv",
        [{"cluster_id": "1200", "volume": "100", "reporter": "F.3d", "page": "500"}],
    )
    stats = import_incremental(csv_file, db)
    assert stats.inserted == 1
    assert stats.corrected == 0
    assert stats.unchanged == 0
    row = db.query(LocalCitationIndex).filter_by(normalized_cite="100 F.3d 500").one()
    assert row.cluster_id == 1200


# ── 13. smart_update_unchanged ────────────────────────────────────────────────


def test_smart_update_unchanged(db, tmp_path):
    # Pre-populate index
    db.add(LocalCitationIndex(
        normalized_cite="200 F.3d 100", cluster_id=2001, source="courtlistener_bulk"
    ))
    db.commit()

    csv_file = _write_csv(
        tmp_path,
        "citations.csv",
        [{"cluster_id": "2001", "volume": "200", "reporter": "F.3d", "page": "100"}],
    )
    stats = import_incremental(csv_file, db)
    assert stats.inserted == 0
    assert stats.corrected == 0
    assert stats.unchanged == 1


# ── 14. smart_update_correction ───────────────────────────────────────────────


def test_smart_update_correction(db, tmp_path):
    # Pre-populate with old cluster_id
    db.add(LocalCitationIndex(
        normalized_cite="300 F.3d 200", cluster_id=3001, source="courtlistener_bulk"
    ))
    db.commit()

    # CSV has updated cluster_id
    csv_file = _write_csv(
        tmp_path,
        "citations.csv",
        [{"cluster_id": "3999", "volume": "300", "reporter": "F.3d", "page": "200"}],
    )
    stats = import_incremental(csv_file, db)
    assert stats.inserted == 0
    assert stats.corrected == 1
    assert stats.unchanged == 0
    row = db.query(LocalCitationIndex).filter_by(normalized_cite="300 F.3d 200").one()
    assert row.cluster_id == 3999


# ── 15. bulk_import_upgrades_user_submitted_to_authoritative ─────────────────


def test_bulk_import_upgrades_user_submitted_to_authoritative(db, tmp_path):
    # User-submitted cache entry for a cite
    _add_cache_entry(
        db,
        normalized_cite="400 F.3d 300",
        cluster_id=4001,
        resolution_method="user",
        trust_tier="user_submitted",
        cache_user_id=55,
    )

    # Bulk CSV that confirms same cluster_id
    csv_file = _write_csv(
        tmp_path,
        "citations.csv",
        [{"cluster_id": "4001", "volume": "400", "reporter": "F.3d", "page": "300"}],
    )
    stats = import_incremental(csv_file, db)
    assert stats.upgraded_to_authoritative == 1

    cache_entry = db.query(CitationResolutionCache).filter_by(normalized_cite="400 F.3d 300").one()
    assert cache_entry.trust_tier == "authoritative"


# ── 16. get_cache_suggestions ─────────────────────────────────────────────────


def test_get_cache_suggestions(db):
    # Other user's submission
    _add_cache_entry(
        db,
        normalized_cite="500 F.3d 400",
        cluster_id=5001,
        resolution_method="user",
        trust_tier="user_submitted",
        cache_user_id=20,
    )
    # Current user's own submission (should NOT appear as suggestion)
    _add_cache_entry(
        db,
        normalized_cite="501 F.3d 401",
        cluster_id=5002,
        resolution_method="user",
        trust_tier="user_submitted",
        cache_user_id=99,
    )
    # Algorithmic entry (should NOT appear as suggestion)
    _add_cache_entry(
        db,
        normalized_cite="502 F.3d 402",
        cluster_id=5003,
        resolution_method="heuristic",
        trust_tier="algorithmic",
    )

    current_user_id = 99
    cites = ["500 F.3d 400", "501 F.3d 401", "502 F.3d 402"]
    suggestions = get_cache_suggestions(db, cites, current_user_id)

    # Only other user's submission appears
    assert "500 F.3d 400" in suggestions
    assert "501 F.3d 401" not in suggestions
    assert "502 F.3d 402" not in suggestions
    assert suggestions["500 F.3d 400"]["trust_tier"] == "user_submitted"
