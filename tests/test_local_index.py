"""Tests for app.services.local_index — local citation index lookup and import."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from aaa_db.models import Base, LocalCitationIndex
from app.services.local_index import (
    LocalIndexLookup,
    _build_cite_string,
    _detect_format,
    _normalize_reporter,
    _parse_citation_array,
    clear_index,
    get_stats,
    import_from_csv,
)

# ── In-memory SQLite fixture ──────────────────────────────────────────────────


@pytest.fixture()
def db_session():
    """In-memory SQLite session with the full schema."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    engine.dispose()


# ── Helper to write a temp CSV file ──────────────────────────────────────────


def _write_csv(tmp_path: Path, filename: str, rows: list[dict]) -> Path:
    fp = tmp_path / filename
    with open(fp, "w", newline="", encoding="utf-8") as fh:
        if rows:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    return fp


# ── _normalize_reporter ───────────────────────────────────────────────────────


def test_normalize_reporter_strips_spaces():
    assert _normalize_reporter("U. S.") == "U.S."


def test_normalize_reporter_already_clean():
    assert _normalize_reporter("F.3d") == "F.3d"


# ── _build_cite_string ────────────────────────────────────────────────────────


def test_build_cite_string_basic():
    assert _build_cite_string("347", "U. S.", "483") == "347 U.S. 483"


def test_build_cite_string_missing_parts():
    assert _build_cite_string("", "U.S.", "483") is None
    assert _build_cite_string("347", "", "483") is None
    assert _build_cite_string("347", "U.S.", "") is None


# ── _parse_citation_array ─────────────────────────────────────────────────────


def test_parse_citation_array_json():
    result = _parse_citation_array('["347 U.S. 483", "74 S. Ct. 686"]')
    assert result == ["347 U.S. 483", "74 S. Ct. 686"]


def test_parse_citation_array_pg_quoted():
    result = _parse_citation_array('{"347 U.S. 483","74 S. Ct. 686"}')
    assert "347 U.S. 483" in result
    assert "74 S. Ct. 686" in result


def test_parse_citation_array_pg_unquoted():
    result = _parse_citation_array("{347 U.S. 483}")
    assert result == ["347 U.S. 483"]


def test_parse_citation_array_empty():
    assert _parse_citation_array("") == []
    assert _parse_citation_array("{}") == []
    assert _parse_citation_array("\\N") == []


def test_parse_citation_array_single_string():
    # Bare string (not an array) treated as single citation
    result = _parse_citation_array("347 U.S. 483")
    assert result == ["347 U.S. 483"]


# ── _detect_format ────────────────────────────────────────────────────────────


def test_detect_format_citations():
    header = ["id", "cluster_id", "volume", "reporter", "page", "type"]
    assert _detect_format(header) == "citations"


def test_detect_format_clusters():
    header = ["id", "case_name", "date_filed", "citations", "docket_id"]
    assert _detect_format(header) == "clusters"


def test_detect_format_unknown_raises():
    with pytest.raises(ValueError, match="Unrecognised CSV format"):
        _detect_format(["foo", "bar", "baz"])


# ── import_from_csv — citations format ───────────────────────────────────────


def test_import_citations_format(db_session, tmp_path):
    rows = [
        {"cluster_id": "999", "volume": "347", "reporter": "U.S.", "page": "483", "type": "10"},
        {"cluster_id": "1001", "volume": "410", "reporter": "U.S.", "page": "113", "type": "10"},
        # Malformed row — missing volume
        {"cluster_id": "1002", "volume": "", "reporter": "U.S.", "page": "99", "type": "10"},
    ]
    fp = _write_csv(tmp_path, "citations.csv", rows)
    stats = import_from_csv(fp, db_session)

    assert stats.citations_indexed == 2
    assert stats.clusters_processed == 3

    hit = db_session.query(LocalCitationIndex).filter_by(normalized_cite="347 U.S. 483").first()
    assert hit is not None
    assert hit.cluster_id == 999
    assert hit.volume == 347
    assert hit.reporter == "U.S."
    assert hit.page == "483"


def test_import_citations_format_enriched_with_case_metadata(db_session, tmp_path):
    citations_rows = [
        {"cluster_id": "999", "volume": "347", "reporter": "U.S.", "page": "483", "type": "10"},
    ]
    clusters_rows = [
        {
            "id": "999",
            "case_name": "Brown v. Board of Education",
            "date_filed": "1954-05-17",
            "docket__court_id": "scotus",
            "citations": '["347 U.S. 483"]',
        }
    ]
    citations_fp = _write_csv(tmp_path, "citations.csv", citations_rows)
    clusters_fp = _write_csv(tmp_path, "opinion-clusters.csv", clusters_rows)

    import_from_csv(citations_fp, db_session, case_lookup_filepath=clusters_fp)

    hit = db_session.query(LocalCitationIndex).filter_by(normalized_cite="347 U.S. 483").first()
    assert hit is not None
    assert hit.case_name == "Brown v. Board of Education"
    assert hit.court_id == "scotus"
    assert hit.date_filed == "1954-05-17"


# ── import_from_csv — clusters format ────────────────────────────────────────


def test_import_clusters_format(db_session, tmp_path):
    rows = [
        {
            "id": "999",
            "case_name": "Brown v. Board of Education",
            "date_filed": "1954-05-17",
            "docket__court_id": "scotus",
            "citations": '["347 U.S. 483", "74 S. Ct. 686"]',
        },
        {
            "id": "1001",
            "case_name": "Roe v. Wade",
            "date_filed": "1973-01-22",
            "docket__court_id": "scotus",
            "citations": '["410 U.S. 113"]',
        },
    ]
    fp = _write_csv(tmp_path, "opinion-clusters.csv", rows)
    stats = import_from_csv(fp, db_session)

    assert stats.citations_indexed == 3  # 2 + 1
    assert stats.clusters_processed == 2

    hit = db_session.query(LocalCitationIndex).filter_by(normalized_cite="347 U.S. 483").first()
    assert hit is not None
    assert hit.cluster_id == 999
    assert hit.case_name == "Brown v. Board of Education"

    hit2 = db_session.query(LocalCitationIndex).filter_by(normalized_cite="74 S. Ct. 686").first()
    assert hit2 is not None
    assert hit2.cluster_id == 999  # same cluster, parallel citation


def test_import_clusters_format_skips_empty_citations(db_session, tmp_path):
    rows = [
        {
            "id": "1",
            "case_name": "No Cite Case",
            "date_filed": "2000-01-01",
            "docket__court_id": "",
            "citations": "",
        },
        {
            "id": "2",
            "case_name": "Has Cite",
            "date_filed": "2000-01-01",
            "docket__court_id": "",
            "citations": '["42 U.S.C. 1983"]',
        },
    ]
    fp = _write_csv(tmp_path, "opinion-clusters.csv", rows)
    stats = import_from_csv(fp, db_session)
    # Only the second row has usable citations
    assert stats.citations_indexed == 1


# ── Duplicate handling ────────────────────────────────────────────────────────


def test_reimport_updates_existing_row(db_session, tmp_path):
    rows_v1 = [
        {"cluster_id": "999", "volume": "347", "reporter": "U.S.", "page": "483", "type": "10"},
    ]
    rows_v2 = [
        # Same cite, different cluster (simulating a re-import with updated data)
        {"cluster_id": "1234", "volume": "347", "reporter": "U.S.", "page": "483", "type": "10"},
    ]
    fp1 = _write_csv(tmp_path, "citations_v1.csv", rows_v1)
    fp2 = _write_csv(tmp_path, "citations_v2.csv", rows_v2)

    import_from_csv(fp1, db_session)
    import_from_csv(fp2, db_session)

    hits = db_session.query(LocalCitationIndex).filter_by(normalized_cite="347 U.S. 483").all()
    assert len(hits) == 1  # no duplicate
    assert hits[0].cluster_id == 1234  # updated to v2


def test_duplicate_within_same_import_is_deduplicated(db_session, tmp_path):
    rows = [
        {"cluster_id": "999", "volume": "347", "reporter": "U.S.", "page": "483", "type": "10"},
        {"cluster_id": "999", "volume": "347", "reporter": "U.S.", "page": "483", "type": "10"},
    ]
    fp = _write_csv(tmp_path, "citations.csv", rows)
    stats = import_from_csv(fp, db_session)

    assert stats.duplicates_skipped == 1
    assert stats.citations_indexed == 1


# ── LocalIndexLookup ─────────────────────────────────────────────────────────


def test_lookup_returns_match(db_session):
    db_session.add(
        LocalCitationIndex(
            normalized_cite="347 U.S. 483",
            cluster_id=999,
            case_name="Brown v. Board of Education",
            court_id="scotus",
            date_filed="1954-05-17",
        )
    )
    db_session.commit()

    lookup = LocalIndexLookup(db_session)
    result = lookup.lookup("347 U.S. 483")
    assert result is not None
    assert result["cluster_id"] == 999
    assert result["case_name"] == "Brown v. Board of Education"
    assert result["court_id"] == "scotus"


def test_lookup_returns_none_for_miss(db_session):
    lookup = LocalIndexLookup(db_session)
    assert lookup.lookup("999 U.S. 999") is None


def test_lookup_batch_returns_found_cites(db_session):
    db_session.add(
        LocalCitationIndex(normalized_cite="347 U.S. 483", cluster_id=999, case_name="Brown")
    )
    db_session.add(
        LocalCitationIndex(normalized_cite="410 U.S. 113", cluster_id=1001, case_name="Roe")
    )
    db_session.commit()

    lookup = LocalIndexLookup(db_session)
    result = lookup.lookup_batch(["347 U.S. 483", "410 U.S. 113", "999 U.S. 999"])

    assert len(result) == 2
    assert result["347 U.S. 483"]["cluster_id"] == 999
    assert result["410 U.S. 113"]["cluster_id"] == 1001
    assert "999 U.S. 999" not in result


def test_lookup_batch_empty_input(db_session):
    lookup = LocalIndexLookup(db_session)
    assert lookup.lookup_batch([]) == {}


def test_is_populated_false_on_empty(db_session):
    lookup = LocalIndexLookup(db_session)
    assert not lookup.is_populated()


def test_is_populated_true_after_insert(db_session):
    db_session.add(LocalCitationIndex(normalized_cite="1 U.S. 1", cluster_id=1))
    db_session.commit()
    lookup = LocalIndexLookup(db_session)
    assert lookup.is_populated()


# ── get_stats ─────────────────────────────────────────────────────────────────


def test_get_stats_empty(db_session):
    stats = get_stats(db_session)
    assert stats["total"] == 0
    assert stats["last_import"] is None


def test_get_stats_after_insert(db_session):
    db_session.add(LocalCitationIndex(normalized_cite="1 U.S. 1", cluster_id=1))
    db_session.add(LocalCitationIndex(normalized_cite="2 U.S. 2", cluster_id=2))
    db_session.commit()
    stats = get_stats(db_session)
    assert stats["total"] == 2
    assert stats["last_import"] is not None


# ── clear_index ───────────────────────────────────────────────────────────────


def test_clear_index(db_session):
    db_session.add(LocalCitationIndex(normalized_cite="1 U.S. 1", cluster_id=1))
    db_session.add(LocalCitationIndex(normalized_cite="2 U.S. 2", cluster_id=2))
    db_session.commit()

    count = clear_index(db_session)
    assert count == 2
    assert db_session.query(LocalCitationIndex).count() == 0


# ── Provenance integration ────────────────────────────────────────────────────


def test_local_index_provenance_label():
    from app.services.provenance import get_provenance

    info = get_provenance("VERIFIED", "local_index")
    assert info.label == "Direct Match (local)"
    assert info.css_class == "provenance-direct"


# ── Pipeline integration — verify_citations skips local index when disabled ──


def test_verify_citations_skips_local_index_when_disabled(db_session):
    """When local_index_enabled=False, local index is not queried."""
    from unittest.mock import MagicMock

    from app.services.audit import CitationResult
    from app.services.verification import verify_citations

    mock_index = MagicMock()
    mock_index.lookup_batch.return_value = {"347 U.S. 483": {"cluster_id": 999}}

    citation = CitationResult(
        raw_text="347 U.S. 483",
        citation_type="FullCaseCitation",
        normalized_text="347 U.S. 483",
    )

    verify_citations(
        [citation],
        courtlistener_token=None,
        verification_base_url="https://example.com/",
        local_index=mock_index,
        local_index_enabled=False,
    )

    mock_index.lookup_batch.assert_not_called()
    assert citation.verification_status == "UNVERIFIED_NO_TOKEN"


def test_verify_citations_uses_local_index_hit(db_session):
    """Citations found in local index are VERIFIED without a CL API call."""
    from unittest.mock import MagicMock

    from app.services.audit import CitationResult
    from app.services.verification import verify_citations

    mock_index = MagicMock()
    mock_index.lookup_batch.return_value = {
        "347 U.S. 483": {
            "cluster_id": 999,
            "case_name": "Brown v. Board of Education",
            "court_id": "scotus",
            "date_filed": "1954-05-17",
        }
    }

    citation = CitationResult(
        raw_text="347 U.S. 483",
        citation_type="FullCaseCitation",
        normalized_text="347 U.S. 483",
    )

    verify_citations(
        [citation],
        courtlistener_token="fake-token",
        verification_base_url="https://example.com/",
        local_index=mock_index,
        local_index_enabled=True,
        # Disable all network passes to ensure only local index is used
        virginia_statute_verification=False,
        federal_statute_verification=False,
        search_fallback_enabled=False,
        cap_fallback_enabled=False,
    )

    assert citation.verification_status == "VERIFIED"
    assert citation.resolution_method == "local_index"
    assert citation.selected_cluster_id == 999
