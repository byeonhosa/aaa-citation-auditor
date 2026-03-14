"""Tests for trust-fixes branch: frozen AI memos and cluster_id validation."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from aaa_db.models import AuditRun, CitationResultRecord
from aaa_db.session import SessionLocal
from app.main import app
from app.services.ai_risk_memo import RiskMemo, memo_from_json, memo_to_json

client = TestClient(app)


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_run(*, memo_json: str | None = None) -> int:
    """Insert a bare AuditRun and return its id."""
    with SessionLocal() as db:
        run = AuditRun(
            source_type="text",
            citation_count=0,
            verified_count=0,
            not_found_count=0,
            ambiguous_count=0,
            derived_count=0,
            statute_count=0,
            error_count=0,
            unverified_no_token_count=0,
            memo_json=memo_json,
        )
        db.add(run)
        db.commit()
        return run.id


def _make_ambiguous_run() -> tuple[int, int]:
    """Create an AMBIGUOUS citation with two candidates; return (run_id, citation_id)."""
    with SessionLocal() as db:
        run = AuditRun(
            source_type="text",
            citation_count=1,
            verified_count=0,
            not_found_count=0,
            ambiguous_count=1,
            derived_count=0,
            statute_count=0,
            error_count=0,
            unverified_no_token_count=0,
        )
        db.add(run)
        db.flush()
        citation = CitationResultRecord(
            audit_run_id=run.id,
            raw_text="410 U.S. 113",
            citation_type="FullCaseCitation",
            verification_status="AMBIGUOUS",
            candidate_cluster_ids=json.dumps([10, 20]),
            candidate_metadata=json.dumps(
                [
                    {
                        "cluster_id": 10,
                        "case_name": "Roe v. Wade",
                        "court": None,
                        "date_filed": "1973-01-22",
                    },
                    {
                        "cluster_id": 20,
                        "case_name": "Other v. Other",
                        "court": None,
                        "date_filed": "1990-01-01",
                    },
                ]
            ),
        )
        db.add(citation)
        db.commit()
        return run.id, citation.id


_STUB_MEMO = RiskMemo(
    risk_level="Low",
    summary="All citations verified.",
    top_issues=[],
    recommended_actions=[],
    advisory_note="Advisory only.",
    available=True,
    generated_by="stub",
)


# ── FIX 1: Frozen AI memos ────────────────────────────────────────────────────


def test_memo_persisted_after_audit() -> None:
    """Running an audit should persist the AI memo to audit_runs.memo_json."""
    from unittest.mock import patch

    with patch("app.routes.pages.generate_ai_memo_for_group", return_value=_STUB_MEMO):
        response = client.post(
            "/audit", data={"pasted_text": "See Roe v. Wade, 410 U.S. 113 (1973)."}
        )

    assert response.status_code == 200

    with SessionLocal() as db:
        run = db.query(AuditRun).order_by(AuditRun.id.desc()).first()
        assert run is not None
        assert run.memo_json is not None
        stored = json.loads(run.memo_json)
        assert stored["risk_level"] == "Low"
        assert stored["summary"] == "All citations verified."


def test_history_detail_loads_persisted_memo_not_regenerated() -> None:
    """History detail must load the stored memo and NOT call generate_ai_memo_for_group."""
    from unittest.mock import patch

    stored_memo = RiskMemo(
        risk_level="Moderate",
        summary="Stored memo text from prior audit.",
        top_issues=["issue1"],
        recommended_actions=[],
        advisory_note="Advisory only.",
        available=True,
        generated_by="stored",
    )
    run_id = _make_run(memo_json=memo_to_json(stored_memo))

    with patch("app.routes.pages.generate_ai_memo_for_group") as mock_gen:
        response = client.get(f"/history/{run_id}")

    assert response.status_code == 200
    assert "Stored memo text from prior audit." in response.text
    mock_gen.assert_not_called()


def test_history_detail_no_memo_shows_regenerate_prompt() -> None:
    """A run with no stored memo should show a 'no memo' message and regenerate button."""
    run_id = _make_run(memo_json=None)

    response = client.get(f"/history/{run_id}")
    assert response.status_code == 200
    assert "No memo was stored" in response.text
    assert "Regenerate memo" in response.text


def test_regenerate_memo_persists_and_redirects() -> None:
    """POST /history/{run_id}/regenerate-memo should re-persist memo and redirect."""
    from unittest.mock import patch

    run_id = _make_run(memo_json=None)

    new_memo = RiskMemo(
        risk_level="High",
        summary="Regenerated memo content.",
        top_issues=[],
        recommended_actions=[],
        advisory_note="Advisory only.",
        available=True,
        generated_by="regen-stub",
    )

    with patch("app.routes.pages.generate_ai_memo_for_group", return_value=new_memo):
        response = client.post(f"/history/{run_id}/regenerate-memo", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/history/{run_id}"

    with SessionLocal() as db:
        run = db.get(AuditRun, run_id)
        assert run is not None
        assert run.memo_json is not None
        persisted = memo_from_json(run.memo_json)
        assert persisted.summary == "Regenerated memo content."
        assert persisted.risk_level == "High"


def test_regenerate_memo_overwrites_existing_memo() -> None:
    """Regenerating replaces an existing stored memo."""
    from unittest.mock import patch

    old_memo = RiskMemo(
        risk_level="Low",
        summary="Old memo.",
        top_issues=[],
        recommended_actions=[],
        advisory_note="Advisory only.",
        available=True,
    )
    run_id = _make_run(memo_json=memo_to_json(old_memo))

    fresh_memo = RiskMemo(
        risk_level="Critical",
        summary="Fresh memo.",
        top_issues=[],
        recommended_actions=[],
        advisory_note="Advisory only.",
        available=True,
    )

    with patch("app.routes.pages.generate_ai_memo_for_group", return_value=fresh_memo):
        client.post(f"/history/{run_id}/regenerate-memo", follow_redirects=False)

    with SessionLocal() as db:
        run = db.get(AuditRun, run_id)
        assert run is not None
        persisted = memo_from_json(run.memo_json)
        assert persisted.summary == "Fresh memo."


# ── memo_to_json / memo_from_json round-trip ──────────────────────────────────


def test_memo_to_json_and_back() -> None:
    """memo_to_json → memo_from_json round-trip preserves all fields."""
    original = RiskMemo(
        risk_level="Moderate",
        summary="Some summary.",
        top_issues=["issue A", "issue B"],
        recommended_actions=["do X"],
        advisory_note="Advisory.",
        available=True,
        unavailable_reason=None,
        generated_by="OpenAI gpt-4o-mini",
    )
    serialized = memo_to_json(original)
    restored = memo_from_json(serialized)

    assert restored.risk_level == original.risk_level
    assert restored.summary == original.summary
    assert restored.top_issues == original.top_issues
    assert restored.recommended_actions == original.recommended_actions
    assert restored.generated_by == original.generated_by
    assert restored.available is True


def test_memo_from_json_unavailable_memo() -> None:
    """Unavailable memos survive the round-trip with available=False."""
    from app.services.ai_risk_memo import unavailable_memo

    memo = unavailable_memo("OpenAI API key is not configured.")
    assert memo_from_json(memo_to_json(memo)).available is False
    assert "API key" in memo_from_json(memo_to_json(memo)).unavailable_reason


# ── FIX 2: Cluster-id validation on resolve endpoint ─────────────────────────


def test_resolve_accepts_valid_cluster_id() -> None:
    """Submitting a cluster_id that is in the candidate list should succeed (303 redirect)."""
    run_id, citation_id = _make_ambiguous_run()
    response = client.post(
        f"/history/{run_id}/citations/{citation_id}/resolve",
        data={"cluster_id": "10"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/history/{run_id}"


def test_resolve_accepts_second_valid_cluster_id() -> None:
    """Both candidates in the list are accepted."""
    run_id, citation_id = _make_ambiguous_run()
    response = client.post(
        f"/history/{run_id}/citations/{citation_id}/resolve",
        data={"cluster_id": "20"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_resolve_rejects_cluster_id_not_in_candidates() -> None:
    """Submitting a cluster_id not in the candidate list must return 400."""
    run_id, citation_id = _make_ambiguous_run()
    response = client.post(
        f"/history/{run_id}/citations/{citation_id}/resolve",
        data={"cluster_id": "999"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "candidate list" in response.text.lower()


def test_resolve_rejects_zero_cluster_id_not_in_candidates() -> None:
    """Zero is a valid integer but not in the candidate list — must return 400."""
    run_id, citation_id = _make_ambiguous_run()
    response = client.post(
        f"/history/{run_id}/citations/{citation_id}/resolve",
        data={"cluster_id": "0"},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_resolve_allows_resolution_with_no_stored_candidates() -> None:
    """Citations with null candidate_cluster_ids (legacy data) should be resolvable."""
    with SessionLocal() as db:
        run = AuditRun(
            source_type="text",
            citation_count=1,
            verified_count=0,
            not_found_count=0,
            ambiguous_count=1,
            derived_count=0,
            statute_count=0,
            error_count=0,
            unverified_no_token_count=0,
        )
        db.add(run)
        db.flush()
        citation = CitationResultRecord(
            audit_run_id=run.id,
            raw_text="410 U.S. 113",
            citation_type="FullCaseCitation",
            verification_status="AMBIGUOUS",
            candidate_cluster_ids=None,  # legacy: no list stored
            candidate_metadata=json.dumps(
                [
                    {
                        "cluster_id": 42,
                        "case_name": "Some v. Case",
                        "court": None,
                        "date_filed": None,
                    }
                ]
            ),
        )
        db.add(citation)
        db.commit()
        run_id = run.id
        citation_id = citation.id

    # Should succeed even without a stored candidate list (no validation possible)
    response = client.post(
        f"/history/{run_id}/citations/{citation_id}/resolve",
        data={"cluster_id": "42"},
        follow_redirects=False,
    )
    assert response.status_code == 303
