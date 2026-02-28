from io import BytesIO

import pytest
from docx import Document
from fastapi.testclient import TestClient

from aaa_db.models import AuditRun, Base, CitationResultRecord
from aaa_db.session import SessionLocal, engine
from app.main import app
from app.services.audit import CitationResult, extract_text_from_docx, resolve_id_citations
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
        db.commit()


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


def test_post_audit_with_pasted_text_shows_results() -> None:
    response = client.post(
        "/audit",
        data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954). Id. at 486."},
    )

    assert response.status_code == 200
    assert "Citations found" in response.text
    assert "Brown v. Board" in response.text


def test_post_audit_empty_input_shows_validation_message() -> None:
    response = client.post("/audit", data={"pasted_text": ""})

    assert response.status_code == 200
    assert "Please provide pasted text or upload a .docx/.pdf file." in response.text


def test_extract_text_from_docx_helper() -> None:
    document = Document()
    document.add_paragraph("Roe v. Wade, 410 U.S. 113 (1973).")
    buffer = BytesIO()
    document.save(buffer)

    extracted_text = extract_text_from_docx(buffer.getvalue())

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
        files={"uploaded_file": ("notes.txt", b"Not a supported file", "text/plain")},
    )

    assert response.status_code == 200
    assert "Unsupported file type. Please upload a .docx or .pdf file." in response.text


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


def test_verify_citations_marks_id_as_derived_not_directly_verified() -> None:
    citations = [CitationResult(raw_text="Id. at 50", citation_type="IdCitation")]

    verified = verify_citations(
        citations,
        courtlistener_token="token",
        verification_base_url="https://example.test/verify",
        verifier=StubVerifiedVerifier(),
    )

    assert verified[0].verification_status == "AMBIGUOUS"
    assert "not directly verified" in (verified[0].verification_detail or "")


def test_dashboard_post_renders_verification_status(monkeypatch) -> None:
    def fake_verify(citations, **kwargs):  # noqa: ANN001, ANN003
        citations[0].verification_status = "VERIFIED"
        citations[0].verification_detail = "Mock verified"
        return citations

    monkeypatch.setattr("app.routes.pages.verify_citations", fake_verify)

    response = client.post(
        "/audit",
        data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954)."},
    )

    assert response.status_code == 200
    assert "VERIFIED" in response.text
    assert "Mock verified" in response.text


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


def test_history_page_shows_saved_runs() -> None:
    client.post("/audit", data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954)."})

    response = client.get("/history")

    assert response.status_code == 200
    assert "Audit History" in response.text
    assert "Brown" not in response.text


def test_history_detail_existing_run_returns_200() -> None:
    client.post("/audit", data={"pasted_text": "Brown v. Board of Educ., 347 U.S. 483 (1954)."})

    with SessionLocal() as db:
        run = db.query(AuditRun).first()

    response = client.get(f"/history/{run.id}")

    assert response.status_code == 200
    assert f"Audit Run #{run.id}" in response.text


def test_history_detail_missing_run_returns_404() -> None:
    response = client.get("/history/999999")

    assert response.status_code == 404


def test_only_excerpt_is_stored_for_pasted_text() -> None:
    long_text = "A" * 500
    client.post("/audit", data={"pasted_text": long_text})

    with SessionLocal() as db:
        run = db.query(AuditRun).first()

    assert run is not None
    assert run.input_text_excerpt == long_text[:200]
    assert run.input_text_excerpt != long_text
