from io import BytesIO

from docx import Document
from fastapi.testclient import TestClient

from app.main import app
from app.services.audit import CitationResult, extract_text_from_docx, resolve_id_citations

client = TestClient(app)


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
