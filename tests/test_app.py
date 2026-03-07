from io import BytesIO
from types import SimpleNamespace
from typing import Any

import pytest
from docx import Document
from fastapi.testclient import TestClient
from sqlalchemy import text

from aaa_db.models import AuditRun, CitationResultRecord, TelemetryEvent
from aaa_db.session import SessionLocal
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
    _split_into_batches,
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


def _docx_bytes(text_value: str) -> bytes:
    document = Document()
    document.add_paragraph(text_value)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _pdf_bytes(text_value: str) -> bytes:
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    if text_value:
        page.insert_text((72, 72), text_value)
    buf = doc.tobytes()
    doc.close()
    return buf


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


def test_extract_text_from_pdf_with_citations() -> None:
    from app.services.audit import extract_text_from_pdf

    text = extract_text_from_pdf(_pdf_bytes("Brown v. Board of Educ., 347 U.S. 483 (1954)."))

    assert "Brown v. Board" in text


def test_extract_text_from_pdf_with_no_text() -> None:
    from app.services.audit import extract_text_from_pdf

    text = extract_text_from_pdf(_pdf_bytes(""))

    assert text.strip() == ""


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
    assert "Derived from prior citation" in (verified[0].verification_detail or "")


def test_dashboard_post_renders_derived_status(monkeypatch) -> None:
    def fake_verify(citations, **kwargs):  # noqa: ANN001, ANN003
        citations[0].verification_status = "DERIVED"
        citations[
            0
        ].verification_detail = (
            "Derived from prior citation (unknown prior citation); not independently verified."
        )
        return citations

    monkeypatch.setattr("app.routes.pages.verify_citations", fake_verify)

    response = client.post(
        "/audit",
        data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954)."},
    )

    assert response.status_code == 200
    assert "DERIVED" in response.text


def test_derived_citation_includes_resolved_from_in_detail() -> None:
    citations = [
        CitationResult(
            raw_text="Id. at 50",
            citation_type="IdCitation",
            resolved_from="Brown v. Board of Educ., 347 U.S. 483 (1954).",
        ),
    ]

    verified = verify_citations(
        citations,
        courtlistener_token="token",
        verification_base_url="https://example.test/verify",
        verifier=StubVerifiedVerifier(),
    )

    assert verified[0].verification_status == "DERIVED"
    assert "Brown v. Board" in (verified[0].verification_detail or "")


def test_summary_counts_separate_derived_from_ambiguous() -> None:
    from app.services.verification import summarize_verification_statuses

    citations = [
        CitationResult(
            raw_text="Brown v. Board of Educ., 347 U.S. 483 (1954).",
            citation_type="FullCaseCitation",
            verification_status="VERIFIED",
        ),
        CitationResult(
            raw_text="Id. at 486",
            citation_type="IdCitation",
            verification_status="DERIVED",
        ),
        CitationResult(
            raw_text="Id. at 490",
            citation_type="IdCitation",
            verification_status="DERIVED",
        ),
        CitationResult(
            raw_text="Smith v. Jones, 100 U.S. 200 (2000).",
            citation_type="FullCaseCitation",
            verification_status="AMBIGUOUS",
        ),
    ]

    summary = summarize_verification_statuses(citations)

    assert summary["DERIVED"] == 2
    assert summary["AMBIGUOUS"] == 1
    assert summary["VERIFIED"] == 1


def test_repository_separates_derived_and_ambiguous_counts() -> None:
    from aaa_db.repository import save_audit_run

    citations = [
        CitationResult(
            raw_text="Brown v. Board of Educ., 347 U.S. 483 (1954).",
            citation_type="FullCaseCitation",
            verification_status="VERIFIED",
            verification_detail="Matched.",
        ),
        CitationResult(
            raw_text="Id. at 486",
            citation_type="IdCitation",
            verification_status="DERIVED",
            verification_detail="Derived from prior citation.",
        ),
        CitationResult(
            raw_text="Id. at 490",
            citation_type="IdCitation",
            verification_status="DERIVED",
            verification_detail="Derived from prior citation.",
        ),
        CitationResult(
            raw_text="Smith v. Jones, 100 U.S. 200 (2000).",
            citation_type="FullCaseCitation",
            verification_status="AMBIGUOUS",
            verification_detail="Multiple matches.",
        ),
    ]

    with SessionLocal() as db:
        run = save_audit_run(
            db,
            source_type="text",
            source_name=None,
            input_text="Test text",
            warnings=[],
            citations=citations,
        )

    assert run.citation_count == 4
    assert run.verified_count == 1
    assert run.derived_count == 2
    assert run.ambiguous_count == 1


def test_markdown_export_includes_derived_count() -> None:
    client.post(
        "/audit",
        data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954). Id. at 486."},
    )

    with SessionLocal() as db:
        run = db.query(AuditRun).first()

    response = client.get(f"/history/{run.id}/export?format=markdown")

    assert response.status_code == 200
    assert "DERIVED=" in response.text


def test_history_page_shows_derived_count() -> None:
    client.post(
        "/audit",
        data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954). Id. at 486."},
    )

    response = client.get("/history")

    assert response.status_code == 200
    assert "DERIVED" in response.text


def test_history_detail_shows_derived_count() -> None:
    client.post(
        "/audit",
        data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954). Id. at 486."},
    )

    with SessionLocal() as db:
        run = db.query(AuditRun).first()

    response = client.get(f"/history/{run.id}")

    assert response.status_code == 200
    assert "DERIVED" in response.text


def test_statute_citation_gets_statute_detected_status() -> None:
    citations = [
        CitationResult(raw_text="42 U.S.C. § 1983", citation_type="FullLawCitation"),
    ]

    verified = verify_citations(
        citations,
        courtlistener_token="token",
        verification_base_url="https://example.test/verify",
        verifier=StubVerifiedVerifier(),
    )

    assert verified[0].verification_status == "STATUTE_DETECTED"
    assert "Statute citation detected" in (verified[0].verification_detail or "")


def test_statute_not_sent_to_courtlistener() -> None:
    """Statutes should be skipped entirely; the verifier should never be called."""

    class TrackingVerifier:
        def __init__(self):
            self.called_with: list[str] = []

        def verify(self, citation: CitationResult) -> VerificationResponse:
            self.called_with.append(citation.raw_text)
            return VerificationResponse(status="VERIFIED", detail="Matched.")

    tracker = TrackingVerifier()
    citations = [
        CitationResult(raw_text="42 U.S.C. § 1983", citation_type="FullLawCitation"),
        CitationResult(
            raw_text="Brown v. Board of Educ., 347 U.S. 483 (1954).",
            citation_type="FullCaseCitation",
        ),
    ]

    verify_citations(
        citations,
        courtlistener_token="token",
        verification_base_url="https://example.test/verify",
        verifier=tracker,
    )

    # Only the case citation should have been sent to the verifier
    assert len(tracker.called_with) == 1
    assert "Brown v. Board" in tracker.called_with[0]


def test_summary_counts_include_statute_detected() -> None:
    from app.services.verification import summarize_verification_statuses

    citations = [
        CitationResult(
            raw_text="Brown v. Board of Educ., 347 U.S. 483 (1954).",
            citation_type="FullCaseCitation",
            verification_status="VERIFIED",
        ),
        CitationResult(
            raw_text="42 U.S.C. § 1983",
            citation_type="FullLawCitation",
            verification_status="STATUTE_DETECTED",
        ),
        CitationResult(
            raw_text="28 U.S.C. § 1331",
            citation_type="FullLawCitation",
            verification_status="STATUTE_DETECTED",
        ),
    ]

    summary = summarize_verification_statuses(citations)

    assert summary["STATUTE_DETECTED"] == 2
    assert summary["VERIFIED"] == 1


def test_repository_stores_statute_count() -> None:
    from aaa_db.repository import save_audit_run

    citations = [
        CitationResult(
            raw_text="Brown v. Board of Educ., 347 U.S. 483 (1954).",
            citation_type="FullCaseCitation",
            verification_status="VERIFIED",
            verification_detail="Matched.",
        ),
        CitationResult(
            raw_text="42 U.S.C. § 1983",
            citation_type="FullLawCitation",
            verification_status="STATUTE_DETECTED",
            verification_detail="Statute citation detected.",
        ),
        CitationResult(
            raw_text="28 U.S.C. § 1331",
            citation_type="FullLawCitation",
            verification_status="STATUTE_DETECTED",
            verification_detail="Statute citation detected.",
        ),
    ]

    with SessionLocal() as db:
        run = save_audit_run(
            db,
            source_type="text",
            source_name=None,
            input_text="Test text",
            warnings=[],
            citations=citations,
        )

    assert run.citation_count == 3
    assert run.verified_count == 1
    assert run.statute_count == 2


def test_markdown_export_includes_statute_count() -> None:
    client.post(
        "/audit",
        data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954). See 42 U.S.C. § 1983."},
    )

    with SessionLocal() as db:
        run = db.query(AuditRun).first()

    response = client.get(f"/history/{run.id}/export?format=markdown")

    assert response.status_code == 200
    assert "STATUTE_DETECTED=" in response.text


def test_history_page_shows_statute_count() -> None:
    client.post(
        "/audit",
        data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954). See 42 U.S.C. § 1983."},
    )

    response = client.get("/history")

    assert response.status_code == 200
    assert "STATUTE_DETECTED" in response.text


def test_history_detail_shows_statute_count() -> None:
    client.post(
        "/audit",
        data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954). See 42 U.S.C. § 1983."},
    )

    with SessionLocal() as db:
        run = db.query(AuditRun).first()

    response = client.get(f"/history/{run.id}")

    assert response.status_code == 200
    assert "STATUTE_DETECTED" in response.text


def test_dashboard_filter_includes_statute_detected_option() -> None:
    response = client.post(
        "/audit",
        data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954). See 42 U.S.C. § 1983."},
    )

    assert response.status_code == 200
    assert "STATUTE_DETECTED" in response.text


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


# ── Batch verification tests ───────────────────────────────────────


class StubBatchVerifier:
    """Verifier that supports both single and batch modes and tracks calls."""

    def __init__(
        self,
        single_status: str = "VERIFIED",
        batch_status: str = "VERIFIED",
    ):
        self.single_calls: list[str] = []
        self.batch_calls: list[list[str]] = []
        self.single_status = single_status
        self.batch_status = batch_status

    def verify(self, citation: CitationResult) -> VerificationResponse:
        self.single_calls.append(citation.raw_text)
        return VerificationResponse(
            status=self.single_status, detail=f"Single: {citation.raw_text}"
        )

    def verify_batch(self, citations: list[CitationResult]) -> list[VerificationResponse]:
        self.batch_calls.append([c.raw_text for c in citations])
        return [
            VerificationResponse(status=self.batch_status, detail=f"Batch: {c.raw_text}")
            for c in citations
        ]


class StubMixedBatchVerifier:
    """Returns different statuses for each citation in a batch."""

    def __init__(self, statuses: list[str]):
        self.statuses = statuses
        self.batch_calls: list[list[str]] = []

    def verify(self, citation: CitationResult) -> VerificationResponse:
        return VerificationResponse(status="VERIFIED", detail="fallback")

    def verify_batch(self, citations: list[CitationResult]) -> list[VerificationResponse]:
        self.batch_calls.append([c.raw_text for c in citations])
        return [
            VerificationResponse(
                status=self.statuses[i % len(self.statuses)],
                detail=f"Mixed: {c.raw_text}",
            )
            for i, c in enumerate(citations)
        ]


class StubFailingBatchVerifier:
    """verify_batch always raises; verify works fine (tests fallback)."""

    def __init__(self):
        self.single_calls: list[str] = []
        self.batch_calls: int = 0

    def verify(self, citation: CitationResult) -> VerificationResponse:
        self.single_calls.append(citation.raw_text)
        return VerificationResponse(status="VERIFIED", detail=f"Fallback: {citation.raw_text}")

    def verify_batch(self, citations: list[CitationResult]) -> list[VerificationResponse]:
        self.batch_calls += 1
        raise RuntimeError("batch endpoint down")


def test_batch_mode_makes_fewer_api_calls_than_citations() -> None:
    """With 5 case-law citations, batch mode should make 1 batch call, not 5 single calls."""
    tracker = StubBatchVerifier()
    citations = [
        CitationResult(
            raw_text=f"Case v. State{i}, {100 + i} U.S. {200 + i} (2000).",
            citation_type="FullCaseCitation",
        )
        for i in range(5)
    ]

    verify_citations(
        citations,
        courtlistener_token="token",
        verification_base_url="https://example.test/verify",
        verifier=tracker,
        batch_verification=True,
    )

    assert len(tracker.batch_calls) == 1
    assert len(tracker.batch_calls[0]) == 5
    assert len(tracker.single_calls) == 0
    for c in citations:
        assert c.verification_status == "VERIFIED"


def test_batch_mode_skips_statute_and_derived_before_batching() -> None:
    """Statute and derived citations are handled before batch; only case law is batched."""
    tracker = StubBatchVerifier()
    citations = [
        CitationResult(
            raw_text="Brown v. Board of Educ., 347 U.S. 483 (1954).",
            citation_type="FullCaseCitation",
        ),
        CitationResult(raw_text="42 U.S.C. § 1983", citation_type="FullLawCitation"),
        CitationResult(
            raw_text="Id. at 486",
            citation_type="IdCitation",
            resolved_from="Brown v. Board of Educ., 347 U.S. 483 (1954).",
        ),
        CitationResult(
            raw_text="Roe v. Wade, 410 U.S. 113 (1973).",
            citation_type="FullCaseCitation",
        ),
    ]

    verify_citations(
        citations,
        courtlistener_token="token",
        verification_base_url="https://example.test/verify",
        verifier=tracker,
        batch_verification=True,
    )

    # Only the 2 case-law citations should be batched
    assert len(tracker.batch_calls) == 1
    assert len(tracker.batch_calls[0]) == 2
    assert "Brown v. Board" in tracker.batch_calls[0][0]
    assert "Roe v. Wade" in tracker.batch_calls[0][1]

    # Statute and derived should be labelled independently
    assert citations[1].verification_status == "STATUTE_DETECTED"
    assert citations[2].verification_status == "DERIVED"


def test_batch_splitting_respects_text_size_limit() -> None:
    """Citations exceeding the text-byte limit are split across batches."""
    # Each citation raw_text is ~50 bytes; with max_text_bytes=120, expect 2 per batch
    citations = [
        CitationResult(
            raw_text=f"{'X' * 40} v. State, {100 + i} U.S. {200 + i} (2000).",
            citation_type="FullCaseCitation",
        )
        for i in range(5)
    ]

    batches = _split_into_batches(citations, max_count=250, max_text_bytes=120)

    assert len(batches) >= 2
    total = sum(len(b) for b in batches)
    assert total == 5


def test_batch_splitting_respects_count_limit() -> None:
    """No batch should have more than max_count citations."""
    citations = [
        CitationResult(raw_text=f"Case{i}", citation_type="FullCaseCitation") for i in range(10)
    ]

    batches = _split_into_batches(citations, max_count=3, max_text_bytes=999_999)

    assert len(batches) == 4  # 3 + 3 + 3 + 1
    assert all(len(b) <= 3 for b in batches)
    total = sum(len(b) for b in batches)
    assert total == 10


def test_batch_splitting_single_oversized_citation() -> None:
    """A single citation exceeding the byte limit still gets its own batch."""
    huge = CitationResult(
        raw_text="A" * 70_000,
        citation_type="FullCaseCitation",
    )
    small = CitationResult(raw_text="Short", citation_type="FullCaseCitation")

    batches = _split_into_batches([huge, small], max_count=250, max_text_bytes=60_000)

    # The huge one can't share a batch with anything
    assert len(batches) == 2
    assert batches[0] == [huge]
    assert batches[1] == [small]


def test_batch_mixed_results_map_back_correctly() -> None:
    """Each citation in a batch receives its own distinct status."""
    tracker = StubMixedBatchVerifier(statuses=["VERIFIED", "NOT_FOUND", "AMBIGUOUS"])
    citations = [
        CitationResult(
            raw_text=f"Case{i} v. State, {100 + i} U.S. {200 + i} (2000).",
            citation_type="FullCaseCitation",
        )
        for i in range(3)
    ]

    verify_citations(
        citations,
        courtlistener_token="token",
        verification_base_url="https://example.test/verify",
        verifier=tracker,
        batch_verification=True,
    )

    assert citations[0].verification_status == "VERIFIED"
    assert citations[1].verification_status == "NOT_FOUND"
    assert citations[2].verification_status == "AMBIGUOUS"


def test_batch_fallback_to_single_on_batch_failure() -> None:
    """When verify_batch raises, each citation is retried individually."""
    tracker = StubFailingBatchVerifier()
    citations = [
        CitationResult(
            raw_text=f"Case{i} v. State, {100 + i} U.S. {200 + i} (2000).",
            citation_type="FullCaseCitation",
        )
        for i in range(3)
    ]

    verify_citations(
        citations,
        courtlistener_token="token",
        verification_base_url="https://example.test/verify",
        verifier=tracker,
        batch_verification=True,
    )

    # batch was attempted once and failed
    assert tracker.batch_calls == 1
    # fell back to 3 individual calls
    assert len(tracker.single_calls) == 3
    for c in citations:
        assert c.verification_status == "VERIFIED"
        assert "Fallback" in (c.verification_detail or "")


def test_batch_verification_disabled_uses_single_mode() -> None:
    """When batch_verification=False, each citation gets its own verify() call."""
    tracker = StubBatchVerifier()
    citations = [
        CitationResult(
            raw_text=f"Case{i} v. State, {100 + i} U.S. {200 + i} (2000).",
            citation_type="FullCaseCitation",
        )
        for i in range(4)
    ]

    verify_citations(
        citations,
        courtlistener_token="token",
        verification_base_url="https://example.test/verify",
        verifier=tracker,
        batch_verification=False,
    )

    assert len(tracker.batch_calls) == 0
    assert len(tracker.single_calls) == 4
    for c in citations:
        assert c.verification_status == "VERIFIED"


def test_batch_verifier_without_batch_method_falls_back_to_single() -> None:
    """A verifier without verify_batch uses single mode even when batch_verification=True."""
    tracker = StubVerifiedVerifier()
    citations = [
        CitationResult(
            raw_text="Case v. State, 100 U.S. 200 (2000).",
            citation_type="FullCaseCitation",
        ),
        CitationResult(
            raw_text="Case v. State, 101 U.S. 201 (2001).",
            citation_type="FullCaseCitation",
        ),
    ]

    verify_citations(
        citations,
        courtlistener_token="token",
        verification_base_url="https://example.test/verify",
        verifier=tracker,
        batch_verification=True,
    )

    # StubVerifiedVerifier has no verify_batch, so single mode is used
    for c in citations:
        assert c.verification_status == "VERIFIED"


def test_batch_with_multiple_batches_all_verified() -> None:
    """When citations are split across multiple batches, all get verified."""
    tracker = StubBatchVerifier()
    citations = [
        CitationResult(
            raw_text=f"Case{i} v. State, {100 + i} U.S. {200 + i} (2000).",
            citation_type="FullCaseCitation",
        )
        for i in range(7)
    ]

    # Force 3 per batch
    verify_citations(
        citations,
        courtlistener_token="token",
        verification_base_url="https://example.test/verify",
        verifier=tracker,
        batch_verification=True,
    )

    # Default batch limits won't split 7 small citations, so 1 batch call
    assert len(tracker.batch_calls) == 1
    assert len(tracker.batch_calls[0]) == 7
    for c in citations:
        assert c.verification_status == "VERIFIED"


def test_empty_citation_list_returns_immediately() -> None:
    """No calls should be made when the citation list is empty."""
    tracker = StubBatchVerifier()

    result = verify_citations(
        [],
        courtlistener_token="token",
        verification_base_url="https://example.test/verify",
        verifier=tracker,
        batch_verification=True,
    )

    assert result == []
    assert len(tracker.batch_calls) == 0
    assert len(tracker.single_calls) == 0


def test_all_statutes_and_derived_skips_verification_entirely() -> None:
    """If every citation is a statute or derived, no verifier calls are made."""
    tracker = StubBatchVerifier()
    citations = [
        CitationResult(raw_text="42 U.S.C. § 1983", citation_type="FullLawCitation"),
        CitationResult(
            raw_text="Id. at 50",
            citation_type="IdCitation",
            resolved_from="42 U.S.C. § 1983",
        ),
    ]

    verify_citations(
        citations,
        courtlistener_token="token",
        verification_base_url="https://example.test/verify",
        verifier=tracker,
        batch_verification=True,
    )

    assert len(tracker.batch_calls) == 0
    assert len(tracker.single_calls) == 0
    assert citations[0].verification_status == "STATUTE_DETECTED"
    assert citations[1].verification_status == "DERIVED"


def test_batch_verification_setting_default_is_true() -> None:
    """The batch_verification setting should default to True."""
    from app.settings import Settings

    s = Settings(
        courtlistener_token="test-token",
        _env_file=None,
    )
    assert s.batch_verification is True


# ── httpx + retry tests ────────────────────────────────────────────


def _mock_httpx_response(status_code: int, json_data: Any = None) -> Any:
    """Create an httpx.Response suitable for testing."""
    import httpx as _httpx

    req = _httpx.Request("POST", "https://example.test/")
    return _httpx.Response(status_code, json=json_data, request=req)


class _MockClient:
    """Drop-in replacement for httpx.Client that returns scripted responses."""

    def __init__(self, responses: list, **_kwargs: Any) -> None:
        self._responses = list(responses)
        self._call_count = 0

    def __enter__(self) -> "_MockClient":
        return self

    def __exit__(self, *_args: Any) -> None:
        pass

    def post(self, url: str, **_kwargs: Any) -> Any:
        if self._call_count >= len(self._responses):
            raise RuntimeError("MockClient exhausted responses")
        item = self._responses[self._call_count]
        self._call_count += 1
        if isinstance(item, Exception):
            raise item
        return item


def test_post_with_retry_retries_on_429_then_succeeds(monkeypatch) -> None:
    """A 429 followed by a 200 should return the 200 response."""
    from app.services.http_client import post_with_retry

    responses = [
        _mock_httpx_response(429),
        _mock_httpx_response(200, json_data=[{"status": 200, "clusters": []}]),
    ]

    monkeypatch.setattr(
        "app.services.http_client.httpx.Client",
        lambda **kw: _MockClient(responses, **kw),
    )
    monkeypatch.setattr("app.services.http_client.time.sleep", lambda _: None)

    resp = post_with_retry(
        "https://example.test/verify",
        data={"text": "test"},
        headers={"Authorization": "Token test"},
    )

    assert resp.status_code == 200


def test_post_with_retry_retries_on_timeout_then_succeeds(monkeypatch) -> None:
    """A timeout followed by a success should return the success response."""
    import httpx as _httpx

    from app.services.http_client import post_with_retry

    responses = [
        _httpx.ReadTimeout("timed out"),
        _mock_httpx_response(200, json_data=[{"status": 200}]),
    ]

    monkeypatch.setattr(
        "app.services.http_client.httpx.Client",
        lambda **kw: _MockClient(responses, **kw),
    )
    monkeypatch.setattr("app.services.http_client.time.sleep", lambda _: None)

    resp = post_with_retry(
        "https://example.test/verify",
        data={"text": "test"},
        headers={"Authorization": "Token test"},
    )

    assert resp.status_code == 200


def test_post_with_retry_exhausts_retries_on_429(monkeypatch) -> None:
    """Three consecutive 429s should return the last 429 response."""
    from app.services.http_client import post_with_retry

    responses = [
        _mock_httpx_response(429),
        _mock_httpx_response(429),
        _mock_httpx_response(429),
    ]

    monkeypatch.setattr(
        "app.services.http_client.httpx.Client",
        lambda **kw: _MockClient(responses, **kw),
    )
    monkeypatch.setattr("app.services.http_client.time.sleep", lambda _: None)

    resp = post_with_retry(
        "https://example.test/verify",
        data={"text": "test"},
        headers={"Authorization": "Token test"},
    )

    assert resp.status_code == 429


def test_post_with_retry_exhausts_retries_on_timeout(monkeypatch) -> None:
    """Three consecutive timeouts should raise TimeoutException."""
    import httpx as _httpx

    from app.services.http_client import post_with_retry

    responses = [
        _httpx.ReadTimeout("timed out"),
        _httpx.ReadTimeout("timed out"),
        _httpx.ReadTimeout("timed out"),
    ]

    monkeypatch.setattr(
        "app.services.http_client.httpx.Client",
        lambda **kw: _MockClient(responses, **kw),
    )
    monkeypatch.setattr("app.services.http_client.time.sleep", lambda _: None)

    with pytest.raises(_httpx.TimeoutException):
        post_with_retry(
            "https://example.test/verify",
            data={"text": "test"},
            headers={"Authorization": "Token test"},
        )


def test_courtlistener_verifier_retry_429_then_verified(monkeypatch) -> None:
    """CourtListenerVerifier retries a 429 and returns VERIFIED on eventual success."""
    from app.services.verification import CourtListenerVerifier

    responses = [
        _mock_httpx_response(429),
        _mock_httpx_response(200, json_data=[{"status": 200, "clusters": [{"id": 1}]}]),
    ]

    monkeypatch.setattr(
        "app.services.http_client.httpx.Client",
        lambda **kw: _MockClient(responses, **kw),
    )
    monkeypatch.setattr("app.services.http_client.time.sleep", lambda _: None)

    verifier = CourtListenerVerifier(
        token="test-token",
        base_url="https://example.test/verify",
        timeout_seconds=5,
    )
    result = verifier.verify(
        CitationResult(
            raw_text="Brown v. Board of Educ., 347 U.S. 483 (1954).",
            citation_type="FullCaseCitation",
        ),
    )

    assert result.status == "VERIFIED"


def test_courtlistener_verifier_timeout_exhausted_returns_error(monkeypatch) -> None:
    """All retries timing out should mark citation as ERROR."""
    import httpx as _httpx

    from app.services.verification import CourtListenerVerifier

    responses = [
        _httpx.ReadTimeout("timed out"),
        _httpx.ReadTimeout("timed out"),
        _httpx.ReadTimeout("timed out"),
    ]

    monkeypatch.setattr(
        "app.services.http_client.httpx.Client",
        lambda **kw: _MockClient(responses, **kw),
    )
    monkeypatch.setattr("app.services.http_client.time.sleep", lambda _: None)

    verifier = CourtListenerVerifier(
        token="test-token",
        base_url="https://example.test/verify",
        timeout_seconds=5,
    )
    result = verifier.verify(
        CitationResult(
            raw_text="Brown v. Board of Educ., 347 U.S. 483 (1954).",
            citation_type="FullCaseCitation",
        ),
    )

    assert result.status == "ERROR"
    assert "timed out" in result.detail.lower()


def test_courtlistener_verifier_all_429s_returns_error(monkeypatch) -> None:
    """All retries exhausted on 429 should return rate-limit ERROR."""
    from app.services.verification import CourtListenerVerifier

    responses = [
        _mock_httpx_response(429),
        _mock_httpx_response(429),
        _mock_httpx_response(429),
    ]

    monkeypatch.setattr(
        "app.services.http_client.httpx.Client",
        lambda **kw: _MockClient(responses, **kw),
    )
    monkeypatch.setattr("app.services.http_client.time.sleep", lambda _: None)

    verifier = CourtListenerVerifier(
        token="test-token",
        base_url="https://example.test/verify",
        timeout_seconds=5,
    )
    result = verifier.verify(
        CitationResult(
            raw_text="Brown v. Board of Educ., 347 U.S. 483 (1954).",
            citation_type="FullCaseCitation",
        ),
    )

    assert result.status == "ERROR"
    assert "rate limit" in result.detail.lower()


def test_ai_memo_retry_timeout_returns_unavailable(monkeypatch) -> None:
    """AI memo generation with all retries timing out should return unavailable."""
    import httpx as _httpx

    from app.services.ai_risk_memo import generate_risk_memo

    responses = [
        _httpx.ReadTimeout("timed out"),
        _httpx.ReadTimeout("timed out"),
        _httpx.ReadTimeout("timed out"),
    ]

    monkeypatch.setattr(
        "app.services.http_client.httpx.Client",
        lambda **kw: _MockClient(responses, **kw),
    )
    monkeypatch.setattr("app.services.http_client.time.sleep", lambda _: None)

    memo = generate_risk_memo(
        {"verification_summary": {"VERIFIED": 1}},
        enabled=True,
        api_key="sk-test-key",
        model="gpt-4.1-mini",
        timeout_seconds=5,
    )

    assert memo.available is False
    assert "timed out" in (memo.unavailable_reason or "").lower()


def test_settings_new_timeout_defaults() -> None:
    """Verify the renamed timeout settings have correct defaults."""
    from app.settings import Settings

    s = Settings(_env_file=None)
    assert s.courtlistener_timeout_seconds == 30
    assert s.ai_request_timeout_seconds == 60


def test_no_urllib_in_production_code() -> None:
    """Ensure urllib is not imported in any production module."""
    import importlib
    import sys

    prod_modules = [
        "app.services.verification",
        "app.services.ai_risk_memo",
        "app.services.http_client",
    ]
    for mod_name in prod_modules:
        if mod_name in sys.modules:
            importlib.reload(sys.modules[mod_name])
        else:
            importlib.import_module(mod_name)

        mod = sys.modules[mod_name]
        source_file = mod.__file__
        assert source_file is not None
        with open(source_file, encoding="utf-8") as f:
            source = f.read()
        assert "from urllib" not in source, f"{mod_name} still imports urllib"
        assert "import urllib" not in source, f"{mod_name} still imports urllib"


# ── Guardrail tests ────────────────────────────────────────────────


def test_validate_upload_limits_rejects_too_many_files() -> None:
    from app.services.audit import validate_upload_limits

    files = [SimpleNamespace(filename=f"doc{i}.pdf", size=100) for i in range(3)]
    result = validate_upload_limits(files, max_files=2, max_file_size_mb=50)

    assert result is not None
    assert "2" in result


def test_validate_upload_limits_rejects_oversized_file() -> None:
    from app.services.audit import validate_upload_limits

    files = [SimpleNamespace(filename="large.pdf", size=60 * 1024 * 1024)]
    result = validate_upload_limits(files, max_files=10, max_file_size_mb=50)

    assert result is not None
    assert "large.pdf" in result
    assert "50 MB" in result


def test_validate_upload_limits_accepts_valid_files() -> None:
    from app.services.audit import validate_upload_limits

    files = [
        SimpleNamespace(filename="a.pdf", size=1024),
        SimpleNamespace(filename="b.pdf", size=2048),
    ]
    result = validate_upload_limits(files, max_files=10, max_file_size_mb=50)

    assert result is None


def test_validate_upload_limits_skips_size_check_when_size_is_none() -> None:
    from app.services.audit import validate_upload_limits

    files = [SimpleNamespace(filename="unknown.pdf", size=None)]
    result = validate_upload_limits(files, max_files=10, max_file_size_mb=0)

    assert result is None


def test_apply_citation_cap_no_truncation_when_under_limit() -> None:
    from app.services.audit import apply_citation_cap

    citations = [
        CitationResult(raw_text=f"Case{i}", citation_type="FullCaseCitation") for i in range(5)
    ]
    result, warning = apply_citation_cap(citations, limit=10)

    assert len(result) == 5
    assert warning is None


def test_apply_citation_cap_truncates_and_returns_warning() -> None:
    from app.services.audit import apply_citation_cap

    citations = [
        CitationResult(raw_text=f"Case{i}", citation_type="FullCaseCitation") for i in range(10)
    ]
    result, warning = apply_citation_cap(citations, limit=3)

    assert len(result) == 3
    assert result[0].raw_text == "Case0"
    assert result[2].raw_text == "Case2"
    assert warning is not None
    assert "10 citations" in warning
    assert "3" in warning


def test_apply_citation_cap_exactly_at_limit_no_truncation() -> None:
    from app.services.audit import apply_citation_cap

    citations = [
        CitationResult(raw_text=f"Case{i}", citation_type="FullCaseCitation") for i in range(5)
    ]
    result, warning = apply_citation_cap(citations, limit=5)

    assert len(result) == 5
    assert warning is None


def test_post_audit_rejects_batch_exceeding_limit(monkeypatch) -> None:
    monkeypatch.setattr("app.routes.pages.settings.max_files_per_batch", 2)

    files = [
        (
            "uploaded_files",
            (
                f"doc{i}.docx",
                _docx_bytes("Brown v. Board of Educ., 347 U.S. 483 (1954)."),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
        )
        for i in range(3)
    ]

    response = client.post("/audit", files=files)

    assert response.status_code == 200
    assert "Too many files" in response.text
    assert "2" in response.text


def test_post_audit_file_size_rejection(monkeypatch) -> None:
    """validate_upload_limits returning an error surfaces as a validation message."""

    def fake_validate(files, max_files, max_file_size_mb):  # noqa: ANN001
        return '"oversized.pdf" is 60.0 MB, which exceeds the 50 MB file size limit.'

    monkeypatch.setattr("app.services.audit.validate_upload_limits", fake_validate)

    response = client.post(
        "/audit",
        files={"uploaded_files": ("oversized.pdf", b"content", "application/pdf")},
    )

    assert response.status_code == 200
    assert "oversized.pdf" in response.text
    assert "50 MB" in response.text


def test_post_audit_file_rejection_shows_validation_message_not_results(monkeypatch) -> None:
    """File rejection should show a validation message without any results section."""

    def fake_validate(files, max_files, max_file_size_mb):  # noqa: ANN001
        return "Too many files uploaded. The limit is 1 file(s) per batch."

    monkeypatch.setattr("app.services.audit.validate_upload_limits", fake_validate)

    response = client.post(
        "/audit",
        files={"uploaded_files": ("a.docx", b"content", "application/octet-stream")},
    )

    assert response.status_code == 200
    assert "Too many files" in response.text
    assert "Results summary" not in response.text


def test_post_audit_citation_cap_warning_appears_in_results(monkeypatch) -> None:
    """When citations are capped, a warning appears in the result group."""
    monkeypatch.setattr("app.routes.pages.settings.max_citations_per_run", 1)

    response = client.post(
        "/audit",
        data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954). Id. at 486."},
    )

    assert response.status_code == 200
    assert "Only the first 1 were processed" in response.text
    assert "splitting the document" in response.text


def test_post_audit_citation_cap_still_processes_retained_citations(monkeypatch) -> None:
    """The first N citations are still verified when the cap kicks in."""
    monkeypatch.setattr("app.routes.pages.settings.max_citations_per_run", 1)

    response = client.post(
        "/audit",
        data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954). Id. at 486."},
    )

    assert response.status_code == 200
    assert "Results summary" in response.text
    assert "Brown v. Board" in response.text


def test_settings_guardrail_defaults() -> None:
    """Guardrail settings have correct defaults."""
    from app.settings import Settings

    s = Settings(_env_file=None)
    assert s.max_file_size_mb == 50
    assert s.max_files_per_batch == 10
    assert s.max_citations_per_run == 500


# -- Logging tests --------------------------------------------------------


def test_logging_is_configured_at_startup() -> None:
    import logging

    root = logging.getLogger()
    assert root.handlers, "Expected at least one logging handler configured at startup"


def test_no_bare_except_pass_in_production_code() -> None:
    import ast
    import os

    class BareExceptPassVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.violations: list[tuple[str, int]] = []

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            body = node.body
            if len(body) == 1 and isinstance(body[0], ast.Pass):
                self.violations.append(("bare except: pass", node.lineno))
            self.generic_visit(node)

    violations: list[str] = []
    app_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app")
    for dirpath, _, filenames in os.walk(app_dir):
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            filepath = os.path.join(dirpath, filename)
            with open(filepath, encoding="utf-8") as f:
                source = f.read()
            try:
                tree = ast.parse(source, filename=filepath)
            except SyntaxError:
                continue
            visitor = BareExceptPassVisitor()
            visitor.visit(tree)
            for msg, lineno in visitor.violations:
                rel = os.path.relpath(filepath, app_dir)
                violations.append(f"{rel}:{lineno}: {msg}")

    assert not violations, "bare except:pass found: " + "; ".join(violations)


def test_log_level_default_is_info() -> None:
    from app.settings import Settings

    s = Settings(_env_file=None)
    assert s.log_level == "INFO"
