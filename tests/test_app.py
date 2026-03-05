from io import BytesIO
from types import SimpleNamespace

import pytest
from docx import Document
from fastapi.testclient import TestClient
from sqlalchemy import text

from aaa_db.models import AuditRun, Base, CitationResultRecord, TelemetryEvent
from aaa_db.session import SessionLocal, engine
from aaa_db.telemetry_repository import get_or_create_install_id
from app.main import app, create_app
from app.routes.pages import build_ai_memo_input, citation_to_context
from app.services.ai_risk_memo import RiskMemo
from app.services.audit import (
    CitationResult,
    extract_citations,
    extract_text_from_docx,
    resolve_id_citations,
)
from app.services.verification import (
    VerificationResponse,
    map_courtlistener_result,
    verify_citations,
)

client = TestClient(app)


class StubVerifiedVerifier:
    def verify(self, citation: CitationResult) -> VerificationResponse:
        return VerificationResponse(status="VERIFIED", detail=f"Matched {citation.raw_text}")


class StubNotFoundVerifier:
    def verify(self, citation: CitationResult) -> VerificationResponse:
        return VerificationResponse(status="NOT_FOUND", detail="No match found.")


class StubErrorVerifier:
    def verify(self, citation: CitationResult) -> VerificationResponse:
        raise RuntimeError("verification crashed")


@pytest.fixture(autouse=True)
def clean_db() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        db.query(CitationResultRecord).delete()
        db.query(AuditRun).delete()
        db.query(TelemetryEvent).delete()
        db.commit()


def _docx_bytes(text_value: str) -> bytes:
    document = Document()
    document.add_paragraph(text_value)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_app_imports() -> None:
    assert app is not None


def test_healthcheck() -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_html_routes() -> None:
    for route in ["/", "/history", "/settings"]:
        response = client.get(route)
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]


def test_clear_button_ui_presence() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert 'id="clear-pasted-text"' in response.text


def test_base_template_has_no_external_htmx_cdn_script() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "unpkg.com/htmx.org" not in response.text


def test_ai_memo_input_excludes_content_by_default() -> None:
    payload = build_ai_memo_input(
        source_type="text",
        source_name=None,
        verification_summary={"VERIFIED": 1},
        citations=[
            {
                "raw_text": "Sensitive case text",
                "citation_type": "FullCaseCitation",
                "resolved_from": None,
                "verification_status": "VERIFIED",
                "verification_detail": "Matched",
                "snippet": "Sensitive snippet",
            }
        ],
        warnings=["Sensitive warning"],
        include_content=False,
    )

    assert payload["citation_count"] == 1
    assert payload["warnings_present"] is True
    assert "citations" not in payload
    assert "warnings" not in payload


def test_ai_memo_input_can_include_content_when_enabled() -> None:
    payload = build_ai_memo_input(
        source_type="text",
        source_name=None,
        verification_summary={"VERIFIED": 1},
        citations=[
            {
                "raw_text": "Brown v. Board",
                "citation_type": "FullCaseCitation",
                "resolved_from": None,
                "verification_status": "VERIFIED",
                "verification_detail": "Matched",
                "snippet": "Snippet",
            }
        ],
        warnings=["Warning text"],
        include_content=True,
    )

    assert payload["citation_count"] == 1
    assert payload["warnings_present"] is True
    assert payload["citations"][0]["raw_text"] == "Brown v. Board"
    assert payload["warnings"] == ["Warning text"]


def test_post_audit_with_pasted_text_shows_results() -> None:
    response = client.post(
        "/audit",
        data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954). Id. at 486."},
    )

    assert response.status_code == 200
    assert "Results summary" in response.text
    assert "Brown v. Board" in response.text


def test_post_audit_empty_input_shows_validation_message() -> None:
    response = client.post("/audit", data={"pasted_text": ""})

    assert response.status_code == 200
    assert "Please provide pasted text or upload a .docx/.pdf file." in response.text


def test_extract_text_from_docx_helper() -> None:
    extracted_text = extract_text_from_docx(_docx_bytes("Roe v. Wade, 410 U.S. 113 (1973)."))

    assert "Roe v. Wade" in extracted_text


def test_resolve_id_citations_helper() -> None:
    citations = [
        CitationResult(
            raw_text="Roe v. Wade, 410 U.S. 113 (1973).", citation_type="FullCaseCitation"
        ),
        CitationResult(raw_text="Id. at 120.", citation_type="IdCitation"),
    ]

    resolved = resolve_id_citations(citations)

    assert resolved[1].resolved_from == "Roe v. Wade, 410 U.S. 113 (1973)."


def test_post_audit_unsupported_file_type() -> None:
    response = client.post(
        "/audit",
        files={"uploaded_files": ("notes.txt", b"Not a supported file", "text/plain")},
    )

    assert response.status_code == 200
    assert "Unsupported file skipped" in response.text


def test_verify_citations_without_token_marks_unverified() -> None:
    citations = [CitationResult(raw_text="Foo", citation_type="FullCaseCitation")]

    verified = verify_citations(
        citations,
        courtlistener_token=None,
        verification_base_url="https://example.test/verify",
    )

    assert verified[0].verification_status == "UNVERIFIED_NO_TOKEN"


def test_verify_citations_mocked_verified() -> None:
    citations = [CitationResult(raw_text="Foo", citation_type="FullCaseCitation")]

    verified = verify_citations(
        citations,
        courtlistener_token="token",
        verification_base_url="https://example.test/verify",
        verifier=StubVerifiedVerifier(),
    )

    assert verified[0].verification_status == "VERIFIED"


def test_verify_citations_mocked_not_found() -> None:
    citations = [CitationResult(raw_text="Foo", citation_type="FullCaseCitation")]

    verified = verify_citations(
        citations,
        courtlistener_token="token",
        verification_base_url="https://example.test/verify",
        verifier=StubNotFoundVerifier(),
    )

    assert verified[0].verification_status == "NOT_FOUND"


def test_verify_citations_error_path_sets_error() -> None:
    citations = [CitationResult(raw_text="Foo", citation_type="FullCaseCitation")]

    verified = verify_citations(
        citations,
        courtlistener_token="token",
        verification_base_url="https://example.test/verify",
        verifier=StubErrorVerifier(),
    )

    assert verified[0].verification_status == "ERROR"


def test_map_courtlistener_result_payload_status_300() -> None:
    payload_item = {
        "status": 300,
        "error_message": "Multiple citations found",
        "clusters": [{"id": 1}, {"id": 2}],
    }

    result = map_courtlistener_result(payload_item)

    assert result.status == "AMBIGUOUS"
    assert "Multiple" in result.detail


def test_verify_citations_marks_id_as_derived() -> None:
    citations = [CitationResult(raw_text="Id. at 50", citation_type="IdCitation")]

    verified = verify_citations(
        citations,
        courtlistener_token="token",
        verification_base_url="https://example.test/verify",
        verifier=StubVerifiedVerifier(),
    )

    assert verified[0].verification_status == "DERIVED"
    assert "Derived citation" in (verified[0].verification_detail or "")


def test_dashboard_post_renders_derived_status(monkeypatch) -> None:
    def fake_verify(citations, **kwargs):  # noqa: ANN001, ANN003
        citations[0].verification_status = "DERIVED"
        citations[
            0
        ].verification_detail = "Derived citation; not directly verified with CourtListener."
        return citations

    monkeypatch.setattr("app.routes.pages.verify_citations", fake_verify)

    response = client.post(
        "/audit",
        data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954)."},
    )

    assert response.status_code == 200
    assert "DERIVED" in response.text


def test_snippet_context_extraction_is_useful() -> None:
    text_value = (
        "Leading text. Brown v. Board of Educ., 347 U.S. 483 (1954). trailing words for context."
    )
    citations, _ = extract_citations(text_value)

    assert citations
    assert citations[0].snippet is not None
    assert "Leading text" in citations[0].snippet or "trailing words" in citations[0].snippet


def test_multiple_file_upload_processes_more_than_one_valid_file() -> None:
    files = [
        (
            "uploaded_files",
            (
                "a.docx",
                _docx_bytes("Brown v. Board of Educ., 347 U.S. 483 (1954)."),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
        ),
        (
            "uploaded_files",
            (
                "b.docx",
                _docx_bytes("Roe v. Wade, 410 U.S. 113 (1973)."),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
        ),
    ]

    response = client.post("/audit", files=files)

    assert response.status_code == 200
    assert "Results for" in response.text
    with SessionLocal() as db:
        runs = db.query(AuditRun).all()
    assert len(runs) == 2


def test_unsupported_file_in_batch_does_not_block_valid_files() -> None:
    files = [
        ("uploaded_files", ("bad.txt", b"unsupported", "text/plain")),
        (
            "uploaded_files",
            (
                "ok.docx",
                _docx_bytes("Brown v. Board of Educ., 347 U.S. 483 (1954)."),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
        ),
    ]

    response = client.post("/audit", files=files)

    assert response.status_code == 200
    assert "Unsupported file skipped" in response.text
    assert "ok.docx" in response.text

    with SessionLocal() as db:
        runs = db.query(AuditRun).all()
    assert len(runs) == 1


def test_results_are_grouped_by_file_source() -> None:
    files = [
        (
            "uploaded_files",
            (
                "one.docx",
                _docx_bytes("Brown v. Board of Educ., 347 U.S. 483 (1954)."),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
        ),
        (
            "uploaded_files",
            (
                "two.docx",
                _docx_bytes("Roe v. Wade, 410 U.S. 113 (1973)."),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
        ),
    ]

    response = client.post("/audit", files=files)

    assert response.status_code == 200
    assert "Results for" in response.text
    assert "one.docx" in response.text
    assert "two.docx" in response.text


def test_filtering_ui_presence_is_rendered() -> None:
    response = client.post(
        "/audit", data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954)."}
    )

    assert response.status_code == 200
    assert 'id="status-filter"' in response.text
    assert 'id="source-filter"' in response.text


def test_history_detail_includes_export_actions() -> None:
    client.post("/audit", data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954)."})

    with SessionLocal() as db:
        run = db.query(AuditRun).first()

    response = client.get(f"/history/{run.id}")

    assert response.status_code == 200
    assert f"/history/{run.id}/export?format=markdown" in response.text
    assert f"/history/{run.id}/export?format=csv" in response.text


def test_markdown_export_returns_expected_content() -> None:
    client.post("/audit", data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954)."})

    with SessionLocal() as db:
        run = db.query(AuditRun).first()

    response = client.get(f"/history/{run.id}/export?format=markdown")

    assert response.status_code == 200
    assert "text/markdown" in response.headers["content-type"]
    assert f"Audit Run #{run.id}" in response.text
    assert "Raw text" in response.text


def test_csv_export_returns_expected_headers_content() -> None:
    client.post("/audit", data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954)."})

    with SessionLocal() as db:
        run = db.query(AuditRun).first()

    response = client.get(f"/history/{run.id}/export?format=csv")

    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "source,raw_text,citation_type" in response.text


def test_printable_html_export_route_returns_html() -> None:
    client.post("/audit", data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954)."})

    with SessionLocal() as db:
        run = db.query(AuditRun).first()

    response = client.get(f"/history/{run.id}/export?format=html")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "AAA Export" in response.text


def test_dashboard_result_group_includes_export_link_for_current_results() -> None:
    response = client.post(
        "/audit", data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954)."}
    )

    assert response.status_code == 200
    assert "/history/" in response.text
    assert "export?format=markdown" in response.text


def test_successful_audit_persists_run_and_citations() -> None:
    response = client.post(
        "/audit",
        data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954)."},
    )
    assert response.status_code == 200

    with SessionLocal() as db:
        run = db.query(AuditRun).first()
        citation_rows = db.query(CitationResultRecord).all()

    assert run is not None
    assert run.citation_count == len(citation_rows)
    assert len(citation_rows) > 0


def test_citation_to_context_handles_missing_snippet_attr() -> None:
    legacy_like = SimpleNamespace(
        raw_text="Legacy raw",
        citation_type="LegacyType",
        normalized_text=None,
        resolved_from=None,
        verification_status="UNVERIFIED_NO_TOKEN",
        verification_detail="No token",
    )

    context = citation_to_context(legacy_like)

    assert context["snippet"] is None


def test_persisted_citation_row_includes_snippet_data() -> None:
    snippet_text = "Leading text. Brown v. Board of Educ., 347 U.S. 483 (1954). trailing text."
    client.post("/audit", data={"pasted_text": snippet_text})

    with SessionLocal() as db:
        row = db.query(CitationResultRecord).first()

    assert row is not None
    assert row.snippet is not None
    assert len(row.snippet) > 0


def test_history_detail_renders_persisted_snippet() -> None:
    snippet_text = "Leading text. Brown v. Board of Educ., 347 U.S. 483 (1954). trailing text."
    client.post("/audit", data={"pasted_text": snippet_text})

    with SessionLocal() as db:
        run = db.query(AuditRun).first()

    response = client.get(f"/history/{run.id}")

    assert response.status_code == 200
    assert "Saved citations" in response.text


def test_history_page_shows_saved_runs() -> None:
    client.post("/audit", data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954)."})

    response = client.get("/history")

    assert response.status_code == 200
    assert "Audit History" in response.text


def test_history_detail_existing_run_returns_200() -> None:
    client.post("/audit", data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954)."})

    with SessionLocal() as db:
        run = db.query(AuditRun).first()

    response = client.get(f"/history/{run.id}")

    assert response.status_code == 200
    assert f"Audit Run #{run.id}" in response.text


def test_history_detail_missing_run_returns_html_404_page() -> None:
    response = client.get("/history/999999")

    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]
    assert "Page Not Found" in response.text


def test_only_excerpt_is_stored_for_pasted_text() -> None:
    long_text = "A" * 500
    client.post("/audit", data={"pasted_text": long_text})

    with SessionLocal() as db:
        run = db.query(AuditRun).first()

    assert run is not None
    assert run.input_text_excerpt == long_text[:200]
    assert run.input_text_excerpt != long_text


def test_install_id_created_and_reused(tmp_path) -> None:
    install_path = tmp_path / "install_id"

    first = get_or_create_install_id(install_path)
    second = get_or_create_install_id(install_path)

    assert first == second
    assert install_path.exists()


def test_audit_completed_telemetry_stores_safe_aggregate_fields_only() -> None:
    marker_text = "Confidential Client Name XZ-123"
    response = client.post("/audit", data={"pasted_text": marker_text})
    assert response.status_code == 200

    with SessionLocal() as db:
        event = (
            db.query(TelemetryEvent)
            .filter(TelemetryEvent.event_type == "audit_completed")
            .order_by(TelemetryEvent.id.desc())
            .first()
        )

    assert event is not None
    assert event.source_type == "text"
    assert event.citation_count is not None
    assert isinstance(event.had_warning, bool)

    forbidden_fields = [
        "source_name",
        "raw_text",
        "normalized_text",
        "verification_detail",
        "input_text_excerpt",
    ]
    for field in forbidden_fields:
        assert not hasattr(event, field)


def test_telemetry_table_exists_in_sqlite() -> None:
    with SessionLocal() as db:
        rows = db.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='telemetry_events'")
        )
        rows = rows.fetchall()

    assert rows


def test_app_started_telemetry_written_on_app_create() -> None:
    create_app()

    with SessionLocal() as db:
        event = (
            db.query(TelemetryEvent)
            .filter(TelemetryEvent.event_type == "app_started")
            .order_by(TelemetryEvent.id.desc())
            .first()
        )

    assert event is not None


def test_history_view_events_are_recorded() -> None:
    client.post("/audit", data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954)."})

    with SessionLocal() as db:
        run = db.query(AuditRun).first()

    response_history = client.get("/history")
    response_detail = client.get(f"/history/{run.id}")
    response_missing = client.get("/history/999999")

    assert response_history.status_code == 200
    assert response_detail.status_code == 200
    assert response_missing.status_code == 404

    with SessionLocal() as db:
        event_types = [row[0] for row in db.query(TelemetryEvent.event_type).all()]

    assert "history_viewed" in event_types
    assert "history_detail_viewed" in event_types
    assert "missing_run_404" in event_types


def test_dashboard_ai_memo_unavailable_without_key(monkeypatch) -> None:
    monkeypatch.setattr("app.routes.pages.settings.ai_memo_enabled", True)
    monkeypatch.setattr("app.routes.pages.settings.openai_api_key", None)

    response = client.post(
        "/audit", data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954)."}
    )

    assert response.status_code == 200
    assert "AI Risk Memo (Advisory)" in response.text
    assert "OpenAI API key is not configured." in response.text


def test_dashboard_renders_mocked_structured_ai_memo(monkeypatch) -> None:
    def fake_generate(*args, **kwargs):  # noqa: ANN002, ANN003
        return RiskMemo(
            risk_level="Moderate",
            summary="Potential citation risks detected.",
            top_issues=["One NOT_FOUND citation"],
            recommended_actions=["Check reporter and pinpoint citations"],
            advisory_note=(
                "AI analysis is advisory only. Deterministic verification statuses remain the "
                "source of truth."
            ),
        )

    monkeypatch.setattr("app.routes.pages.generate_risk_memo", fake_generate)

    response = client.post(
        "/audit", data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954)."}
    )

    assert response.status_code == 200
    assert "Potential citation risks detected." in response.text
    assert "One NOT_FOUND citation" in response.text


def test_history_detail_renders_mocked_structured_ai_memo(monkeypatch) -> None:
    def fake_generate(*args, **kwargs):  # noqa: ANN002, ANN003
        return RiskMemo(
            risk_level="High",
            summary="Several unresolved citations need immediate review.",
            top_issues=["Derived citation chain may be fragile"],
            recommended_actions=["Manually confirm source authority"],
            advisory_note=(
                "AI analysis is advisory only. Deterministic verification statuses remain the "
                "source of truth."
            ),
        )

    monkeypatch.setattr("app.routes.pages.generate_risk_memo", fake_generate)

    client.post("/audit", data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954)."})
    with SessionLocal() as db:
        run = db.query(AuditRun).first()

    response = client.get(f"/history/{run.id}")

    assert response.status_code == 200
    assert "AI Risk Memo (Advisory)" in response.text
    assert "Several unresolved citations need immediate review." in response.text


def test_ai_memo_failure_does_not_break_page(monkeypatch) -> None:
    def broken_generate(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("boom")

    monkeypatch.setattr("app.routes.pages.generate_risk_memo", broken_generate)

    response = client.post(
        "/audit", data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954)."}
    )

    assert response.status_code == 200
    assert "Results summary" in response.text


def test_ai_memo_addition_does_not_change_deterministic_verification_statuses(monkeypatch) -> None:
    def fake_verify(citations, **kwargs):  # noqa: ANN001, ANN003
        citations[0].verification_status = "VERIFIED"
        citations[0].verification_detail = "Matched"
        return citations

    def fake_generate(*args, **kwargs):  # noqa: ANN002, ANN003
        return RiskMemo(
            risk_level="Low",
            summary="Looks good.",
            top_issues=[],
            recommended_actions=[],
            advisory_note=(
                "AI analysis is advisory only. Deterministic verification statuses remain the "
                "source of truth."
            ),
        )

    monkeypatch.setattr("app.routes.pages.verify_citations", fake_verify)
    monkeypatch.setattr("app.routes.pages.generate_risk_memo", fake_generate)

    response = client.post(
        "/audit", data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954)."}
    )

    assert response.status_code == 200
    assert "VERIFIED" in response.text
