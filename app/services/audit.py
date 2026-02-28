from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import fitz
from docx import Document
from eyecite import get_citations
from fastapi import UploadFile


@dataclass
class CitationResult:
    raw_text: str
    citation_type: str
    normalized_text: str | None = None
    resolved_from: str | None = None


def extract_text_from_docx(file_bytes: bytes) -> str:
    document = Document(BytesIO(file_bytes))
    return "\n".join(paragraph.text for paragraph in document.paragraphs if paragraph.text.strip())


def extract_text_from_pdf(file_bytes: bytes) -> str:
    with fitz.open(stream=file_bytes, filetype="pdf") as pdf:
        return "\n".join(page.get_text("text") for page in pdf)


def _value_or_call(value: Any) -> Any:
    return value() if callable(value) else value


def extract_citations(text: str) -> tuple[list[CitationResult], list[str]]:
    warnings: list[str] = []
    results: list[CitationResult] = []

    if not text.strip():
        return results, ["No text was available to parse for citations."]

    for citation in get_citations(text):
        raw_value = _value_or_call(getattr(citation, "matched_text", None))
        raw_text = str(raw_value).strip() if raw_value else str(citation).strip()
        normalized = _value_or_call(getattr(citation, "corrected_citation", None))
        normalized_text = str(normalized).strip() if normalized else None

        results.append(
            CitationResult(
                raw_text=raw_text or "(unavailable)",
                citation_type=type(citation).__name__,
                normalized_text=normalized_text,
            )
        )

    if not results:
        warnings.append("No citations were detected.")

    return results, warnings


def resolve_id_citations(citations: list[CitationResult]) -> list[CitationResult]:
    last_full_citation: CitationResult | None = None

    for citation in citations:
        is_id = citation.raw_text.lower().startswith(
            "id."
        ) or citation.citation_type.lower().startswith("id")

        if is_id:
            citation.resolved_from = last_full_citation.raw_text if last_full_citation else None
            continue

        last_full_citation = citation

    return citations


async def extract_source_text(
    pasted_text: str | None,
    uploaded_file: UploadFile | None,
) -> tuple[str | None, str | None, list[str], str | None]:
    warnings: list[str] = []

    text = (pasted_text or "").strip()
    if text:
        if uploaded_file and uploaded_file.filename:
            warnings.append("Both text and file were submitted. Text input was used.")
        return text, "text", warnings, None

    if not uploaded_file or not uploaded_file.filename:
        return None, None, warnings, "Please provide pasted text or upload a .docx/.pdf file."

    extension = Path(uploaded_file.filename).suffix.lower()
    file_bytes = await uploaded_file.read()

    if extension == ".docx":
        return extract_text_from_docx(file_bytes), "docx", warnings, None

    if extension == ".pdf":
        return extract_text_from_pdf(file_bytes), "pdf", warnings, None

    return None, None, warnings, "Unsupported file type. Please upload a .docx or .pdf file."
