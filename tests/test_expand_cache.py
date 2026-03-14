"""Tests for expand-resolution-cache: caching all successful verifications."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from aaa_db.models import Base, CitationResolutionCache
from aaa_db.repository import (
    _CACHEABLE_METHODS,
    _RESOLUTION_CONFIDENCE,
    _upsert_resolution_cache,
    get_cache_stats,
    lookup_resolution_cache,
    save_audit_run,
)
from app.main import app
from app.services.audit import CitationResult
from app.services.verification import (
    VerificationResponse,
    map_courtlistener_result,
    verify_citations,
)

client = TestClient(app)


# ── In-memory SQLite DB fixture ───────────────────────────────────────────────


@pytest.fixture
def db_session():
    """Provide a fresh in-memory SQLite session for each test."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


# ── map_courtlistener_result: 200 now includes cluster data ──────────────────


class TestMapCourtlistenerResult200:
    def test_200_with_single_cluster_populates_candidate_metadata(self) -> None:
        """A 200 response with one cluster now returns candidate_metadata."""
        result = map_courtlistener_result(
            {
                "status": 200,
                "clusters": [
                    {
                        "id": 42,
                        "case_name": "Brown v. Board",
                        "court_id": "scotus",
                        "date_filed": "1954-05-17",
                    }
                ],
            }
        )
        assert result.status == "VERIFIED"
        assert result.candidate_cluster_ids == [42]
        assert result.candidate_metadata is not None
        assert len(result.candidate_metadata) == 1
        assert result.candidate_metadata[0]["cluster_id"] == 42
        assert result.candidate_metadata[0]["case_name"] == "Brown v. Board"

    def test_200_with_no_clusters_leaves_metadata_none(self) -> None:
        """A 200 response with empty clusters list leaves metadata None."""
        result = map_courtlistener_result({"status": 200, "clusters": []})
        assert result.status == "VERIFIED"
        assert result.candidate_cluster_ids is None
        assert result.candidate_metadata is None

    def test_200_with_no_cluster_id_leaves_metadata_none(self) -> None:
        """A 200 response with a cluster missing 'id' leaves metadata None."""
        result = map_courtlistener_result(
            {"status": 200, "clusters": [{"case_name": "No ID Case"}]}
        )
        assert result.status == "VERIFIED"
        assert result.candidate_cluster_ids is None

    def test_200_with_multiple_clusters_returns_ambiguous(self) -> None:
        """A 200 response with more than one cluster is treated as AMBIGUOUS."""
        result = map_courtlistener_result(
            {
                "status": 200,
                "clusters": [
                    {
                        "id": 1,
                        "case_name": "Alpha v. Beta",
                        "court_id": "ca1",
                        "date_filed": "2000-01-01",
                    },
                    {
                        "id": 2,
                        "case_name": "Alpha v. Beta",
                        "court_id": "ca2",
                        "date_filed": "2001-01-01",
                    },
                ],
            }
        )
        assert result.status == "AMBIGUOUS"
        assert result.candidate_cluster_ids is not None
        assert set(result.candidate_cluster_ids) == {1, 2}
        assert result.candidate_metadata is not None
        assert len(result.candidate_metadata) == 2


# ── Direct match resolution_method in verify_citations ───────────────────────


class TestDirectMatchResolutionMethod:
    """verify_citations sets resolution_method='direct' for single-cluster VERIFIED hits."""

    def test_direct_match_sets_resolution_method(self) -> None:
        """Single-cluster 200 response → resolution_method='direct', selected_cluster_id set."""

        class _DirectVerifier:
            def verify(self, citation: CitationResult) -> VerificationResponse:
                return VerificationResponse(
                    status="VERIFIED",
                    detail="Matched.",
                    candidate_cluster_ids=[99],
                    candidate_metadata=[
                        {
                            "cluster_id": 99,
                            "case_name": "Smith v. Jones",
                            "court": "ca9",
                            "date_filed": "2001-01-01",
                        }
                    ],
                )

        citation = CitationResult(
            raw_text="Smith v. Jones, 100 F.3d 1", citation_type="FullCaseCitation"
        )
        results = verify_citations(
            [citation],
            courtlistener_token="tok",
            verification_base_url="http://test",
            verifier=_DirectVerifier(),
            search_fallback_enabled=False,
        )
        assert results[0].verification_status == "VERIFIED"
        assert results[0].selected_cluster_id == 99
        assert results[0].resolution_method == "direct"

    def test_verified_without_candidate_data_leaves_resolution_method_none(self) -> None:
        """VERIFIED citation without candidate data keeps resolution_method=None."""

        class _SimpleVerifier:
            def verify(self, citation: CitationResult) -> VerificationResponse:
                return VerificationResponse(status="VERIFIED", detail="Matched.")

        citation = CitationResult(raw_text="100 F.3d 1", citation_type="FullCaseCitation")
        results = verify_citations(
            [citation],
            courtlistener_token="tok",
            verification_base_url="http://test",
            verifier=_SimpleVerifier(),
            search_fallback_enabled=False,
        )
        assert results[0].verification_status == "VERIFIED"
        assert results[0].selected_cluster_id is None
        assert results[0].resolution_method is None


# ── Cache writing in save_audit_run ──────────────────────────────────────────


class TestCacheWritingInSaveAuditRun:
    """save_audit_run caches all successful verifications."""

    def _direct_citation(self, raw: str, cluster_id: int) -> CitationResult:
        c = CitationResult(raw_text=raw, citation_type="FullCaseCitation")
        c.verification_status = "VERIFIED"
        c.selected_cluster_id = cluster_id
        c.resolution_method = "direct"
        c.candidate_metadata = [
            {
                "cluster_id": cluster_id,
                "case_name": "Test v. Case",
                "court": "ca1",
                "date_filed": "2020-01-01",
            }
        ]
        c.normalized_text = raw
        return c

    def test_direct_match_is_cached(self, db_session) -> None:
        """A VERIFIED 'direct' citation is written to the cache."""
        citation = self._direct_citation("Smith v. Jones, 100 F.3d 1", 42)
        save_audit_run(
            db_session,
            source_type="text",
            source_name="test",
            input_text="test",
            warnings=[],
            citations=[citation],
        )
        cache = lookup_resolution_cache(db_session)
        assert "Smith v. Jones, 100 F.3d 1" in cache
        assert cache["Smith v. Jones, 100 F.3d 1"]["cluster_id"] == 42
        assert cache["Smith v. Jones, 100 F.3d 1"]["resolution_method"] == "direct"

    def test_search_fallback_is_cached(self, db_session) -> None:
        """A VERIFIED 'search_fallback' citation is written to the cache."""
        c = CitationResult(raw_text="Doe v. Roe 2019 WL 1234", citation_type="FullCaseCitation")
        c.verification_status = "VERIFIED"
        c.selected_cluster_id = 100
        c.resolution_method = "search_fallback"
        c.candidate_metadata = [
            {
                "cluster_id": 100,
                "case_name": "Doe v. Roe",
                "court": "ca5",
                "date_filed": "2019-06-01",
            }
        ]
        c.normalized_text = "Doe v. Roe 2019 WL 1234"
        save_audit_run(
            db_session,
            source_type="text",
            source_name="test",
            input_text="test",
            warnings=[],
            citations=[c],
        )
        cache = lookup_resolution_cache(db_session)
        assert "Doe v. Roe 2019 WL 1234" in cache
        assert cache["Doe v. Roe 2019 WL 1234"]["resolution_method"] == "search_fallback"

    def test_short_cite_match_is_cached(self, db_session) -> None:
        """A VERIFIED 'short_cite_match' citation is written to the cache."""
        c = CitationResult(raw_text="100 F.3d at 5", citation_type="ShortCaseCitation")
        c.verification_status = "VERIFIED"
        c.selected_cluster_id = 77
        c.resolution_method = "short_cite_match"
        c.candidate_metadata = None
        c.normalized_text = "100 F.3d at 5"
        save_audit_run(
            db_session,
            source_type="text",
            source_name="test",
            input_text="test",
            warnings=[],
            citations=[c],
        )
        cache = lookup_resolution_cache(db_session)
        assert "100 F.3d at 5" in cache
        assert cache["100 F.3d at 5"]["cluster_id"] == 77

    def test_dedup_resolution_is_cached(self, db_session) -> None:
        """A VERIFIED 'dedup' citation is written to the cache."""
        c = CitationResult(raw_text="200 F.3d 300", citation_type="FullCaseCitation")
        c.verification_status = "VERIFIED"
        c.selected_cluster_id = 55
        c.resolution_method = "dedup"
        c.candidate_metadata = [
            {
                "cluster_id": 55,
                "case_name": "Alpha v. Beta",
                "court": "ca2",
                "date_filed": "2000-01-01",
            }
        ]
        c.normalized_text = "200 F.3d 300"
        save_audit_run(
            db_session,
            source_type="text",
            source_name="test",
            input_text="test",
            warnings=[],
            citations=[c],
        )
        cache = lookup_resolution_cache(db_session)
        assert "200 F.3d 300" in cache

    def test_not_found_is_not_cached(self, db_session) -> None:
        """NOT_FOUND citations are never written to the cache."""
        c = CitationResult(
            raw_text="Missing v. Citation, 999 F.3d 1", citation_type="FullCaseCitation"
        )
        c.verification_status = "NOT_FOUND"
        c.selected_cluster_id = None
        c.resolution_method = None
        c.normalized_text = "Missing v. Citation, 999 F.3d 1"
        save_audit_run(
            db_session,
            source_type="text",
            source_name="test",
            input_text="test",
            warnings=[],
            citations=[c],
        )
        cache = lookup_resolution_cache(db_session)
        assert "Missing v. Citation, 999 F.3d 1" not in cache

    def test_cache_hit_is_not_re_cached(self, db_session) -> None:
        """Citations resolved from cache (resolution_method='cache') are not re-written."""
        c = CitationResult(raw_text="100 F.3d 1", citation_type="FullCaseCitation")
        c.verification_status = "VERIFIED"
        c.selected_cluster_id = 30
        c.resolution_method = "cache"
        c.normalized_text = "100 F.3d 1"
        # Pre-populate cache with a user entry
        _upsert_resolution_cache(
            db_session,
            normalized_cite="100 F.3d 1",
            selected_cluster_id=30,
            candidate_metadata=None,
            resolution_method="user",
        )
        db_session.commit()
        save_audit_run(
            db_session,
            source_type="text",
            source_name="test",
            input_text="test",
            warnings=[],
            citations=[c],
        )
        cache = lookup_resolution_cache(db_session)
        # Should still be the user entry, not overwritten with "cache"
        assert cache["100 F.3d 1"]["resolution_method"] == "user"


# ── Confidence ordering ───────────────────────────────────────────────────────


class TestResolutionConfidenceOrdering:
    """_upsert_resolution_cache respects confidence ordering."""

    def test_higher_confidence_overwrites_lower(self, db_session) -> None:
        """Heuristic (4) overwrites direct (3)."""
        _upsert_resolution_cache(
            db_session,
            normalized_cite="100 F.3d 1",
            selected_cluster_id=10,
            candidate_metadata=None,
            resolution_method="direct",
        )
        db_session.commit()
        _upsert_resolution_cache(
            db_session,
            normalized_cite="100 F.3d 1",
            selected_cluster_id=10,
            candidate_metadata=None,
            resolution_method="heuristic",
        )
        db_session.commit()
        row = db_session.scalar(
            select(CitationResolutionCache).where(
                CitationResolutionCache.normalized_cite == "100 F.3d 1"
            )
        )
        assert row is not None
        assert row.resolution_method == "heuristic"

    def test_lower_confidence_does_not_overwrite_higher(self, db_session) -> None:
        """User selection (5) is not overwritten by search_fallback (2)."""
        _upsert_resolution_cache(
            db_session,
            normalized_cite="200 F.3d 1",
            selected_cluster_id=20,
            candidate_metadata=None,
            resolution_method="user",
        )
        db_session.commit()
        _upsert_resolution_cache(
            db_session,
            normalized_cite="200 F.3d 1",
            selected_cluster_id=99,
            candidate_metadata=None,
            resolution_method="search_fallback",
        )
        db_session.commit()
        row = db_session.scalar(
            select(CitationResolutionCache).where(
                CitationResolutionCache.normalized_cite == "200 F.3d 1"
            )
        )
        assert row is not None
        assert row.resolution_method == "user"
        assert row.selected_cluster_id == 20  # not overwritten

    def test_equal_confidence_updates_entry(self, db_session) -> None:
        """Equal confidence (dedup=3 and direct=3) allows update."""
        _upsert_resolution_cache(
            db_session,
            normalized_cite="300 F.3d 1",
            selected_cluster_id=30,
            candidate_metadata=None,
            resolution_method="direct",
        )
        db_session.commit()
        _upsert_resolution_cache(
            db_session,
            normalized_cite="300 F.3d 1",
            selected_cluster_id=31,
            candidate_metadata=None,
            resolution_method="dedup",
        )
        db_session.commit()
        row = db_session.scalar(
            select(CitationResolutionCache).where(
                CitationResolutionCache.normalized_cite == "300 F.3d 1"
            )
        )
        assert row is not None
        assert row.resolution_method == "dedup"


# ── Cache lookup prevents redundant API calls ─────────────────────────────────


class TestCacheHitPreventsApiCall:
    """When a citation is in the cache, verify_citations uses it without calling the verifier."""

    def test_cache_hit_skips_verifier(self) -> None:
        """A cached citation is resolved immediately; the verifier is never called."""
        calls: list[str] = []

        class _TrackingVerifier:
            def verify(self, citation: CitationResult) -> VerificationResponse:
                calls.append(citation.raw_text)
                return VerificationResponse(status="VERIFIED", detail="API call.")

        citation = CitationResult(
            raw_text="Smith v. Jones, 100 F.3d 1",
            citation_type="FullCaseCitation",
        )
        citation.normalized_text = "Smith v. Jones, 100 F.3d 1"

        cache = {
            "Smith v. Jones, 100 F.3d 1": {
                "cluster_id": 42,
                "case_name": "Smith v. Jones",
                "court": "ca9",
                "date_filed": "2001-01-01",
                "resolution_method": "direct",
            }
        }

        results = verify_citations(
            [citation],
            courtlistener_token="tok",
            verification_base_url="http://test",
            verifier=_TrackingVerifier(),
            resolution_cache=cache,
            search_fallback_enabled=False,
        )

        assert results[0].verification_status == "VERIFIED"
        assert results[0].resolution_method == "cache"
        assert results[0].selected_cluster_id == 42
        assert calls == [], "Verifier should NOT have been called for a cached citation"

    def test_non_cached_citation_calls_verifier(self) -> None:
        """A citation not in the cache is passed to the verifier."""
        calls: list[str] = []

        class _TrackingVerifier:
            def verify(self, citation: CitationResult) -> VerificationResponse:
                calls.append(citation.raw_text)
                return VerificationResponse(status="NOT_FOUND", detail="Not found.")

        citation = CitationResult(
            raw_text="Unknown v. Citation, 999 F.3d 1",
            citation_type="FullCaseCitation",
        )
        citation.normalized_text = "Unknown v. Citation, 999 F.3d 1"

        results = verify_citations(
            [citation],
            courtlistener_token="tok",
            verification_base_url="http://test",
            verifier=_TrackingVerifier(),
            resolution_cache={},  # empty cache
            search_fallback_enabled=False,
        )

        assert results[0].verification_status == "NOT_FOUND"
        assert len(calls) == 1


# ── get_cache_stats ───────────────────────────────────────────────────────────


class TestGetCacheStats:
    def test_empty_database(self, db_session) -> None:
        """With no data, stats should show zero entries."""
        stats = get_cache_stats(db_session)
        assert stats["total"] == 0
        assert stats["recent_hits"] == 0
        assert stats["recent_verifiable"] == 0

    def test_counts_cache_entries(self, db_session) -> None:
        """total reflects the number of cache rows."""
        _upsert_resolution_cache(
            db_session,
            normalized_cite="cite1",
            selected_cluster_id=1,
            candidate_metadata=None,
            resolution_method="direct",
        )
        _upsert_resolution_cache(
            db_session,
            normalized_cite="cite2",
            selected_cluster_id=2,
            candidate_metadata=None,
            resolution_method="heuristic",
        )
        db_session.commit()
        stats = get_cache_stats(db_session)
        assert stats["total"] == 2

    def test_hit_rate_from_most_recent_run(self, db_session) -> None:
        """recent_hits and recent_verifiable reflect the most recent audit run."""
        from app.services.audit import CitationResult

        # Create two citations: one cache hit, one direct
        c1 = CitationResult(raw_text="A v. B, 1 F.3d 1", citation_type="FullCaseCitation")
        c1.verification_status = "VERIFIED"
        c1.selected_cluster_id = 1
        c1.resolution_method = "cache"
        c1.normalized_text = "A v. B, 1 F.3d 1"

        c2 = CitationResult(raw_text="C v. D, 2 F.3d 2", citation_type="FullCaseCitation")
        c2.verification_status = "VERIFIED"
        c2.selected_cluster_id = 2
        c2.resolution_method = "direct"
        c2.normalized_text = "C v. D, 2 F.3d 2"
        c2.candidate_metadata = [
            {"cluster_id": 2, "case_name": "C v. D", "court": "ca1", "date_filed": "2020-01-01"}
        ]

        save_audit_run(
            db_session,
            source_type="text",
            source_name="test",
            input_text="test",
            warnings=[],
            citations=[c1, c2],
        )

        stats = get_cache_stats(db_session)
        assert stats["recent_verifiable"] == 2
        assert stats["recent_hits"] == 1


# ── Settings page shows cache stats ──────────────────────────────────────────


class TestSettingsPageCacheStats:
    def test_settings_page_shows_cache_entries(self) -> None:
        """GET /settings contains cache entry count."""
        response = client.get("/settings")
        assert response.status_code == 200
        assert "Cache entries:" in response.text

    def test_settings_page_has_cache_stats_section(self) -> None:
        """Settings page always contains the resolution cache section."""
        response = client.get("/settings")
        assert response.status_code == 200
        assert "Resolution Cache" in response.text


# ── Metadata constants ────────────────────────────────────────────────────────


def test_cacheable_methods_set() -> None:
    """_CACHEABLE_METHODS includes all expected resolution methods."""
    expected = {
        "direct",
        "heuristic",
        "dedup",
        "search_fallback",
        "cap_fallback",
        "local_index",
        "short_cite_match",
    }
    assert _CACHEABLE_METHODS == expected


def test_resolution_confidence_ordering() -> None:
    """user > heuristic > direct == dedup > search_fallback > short_cite_match."""
    assert _RESOLUTION_CONFIDENCE["user"] > _RESOLUTION_CONFIDENCE["heuristic"]
    assert _RESOLUTION_CONFIDENCE["heuristic"] > _RESOLUTION_CONFIDENCE["direct"]
    assert _RESOLUTION_CONFIDENCE["direct"] == _RESOLUTION_CONFIDENCE["dedup"]
    assert _RESOLUTION_CONFIDENCE["dedup"] > _RESOLUTION_CONFIDENCE["search_fallback"]
    assert _RESOLUTION_CONFIDENCE["search_fallback"] > _RESOLUTION_CONFIDENCE["short_cite_match"]
    assert _RESOLUTION_CONFIDENCE["short_cite_match"] > _RESOLUTION_CONFIDENCE["cache"]
