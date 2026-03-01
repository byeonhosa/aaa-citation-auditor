from __future__ import annotations

from dataclasses import dataclass, field
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
    verification_status: str | None = None
    verification_detail: str | None = None
    snippet: str | None = None


@dataclass
class SourceInput:
    source_type: str
    source_name: str | None
    text: str
    warnings: list[str] = field(default_factory=list)


def extract_text_from_docx(file_bytes: bytes) -> str:
    document = Document(BytesIO(file_bytes))
    return "\n".join(paragraph.text for paragraph in document.paragraphs if paragraph.text.strip())


def extract_text_from_pdf(file_bytes: bytes) -> str:
    with fitz.open(stream=file_bytes, filetype="pdf") as pdf:
        return "\n".join(page.get_text("text") for page in pdf)


def _value_or_call(value: Any) -> Any:
    return value() if callable(value) else value


def _build_snippet(text: str, start: int, end: int, window: int = 80) -> str:
    snippet_start = max(0, start - window)
    snippet_end = min(len(text), end + window)
    return text[snippet_start:snippet_end].strip().replace("\n", " ")


def extract_citations(text: str) -> tuple[list[CitationResult], list[str]]:
    warnings: list[str] = []
    results: list[CitationResult] = []

    if not text.strip():
        return results, ["No text was available to parse for citations."]

    search_cursor = 0
    lower_text = text.lower()

    for citation in get_citations(text):
        raw_value = _value_or_call(getattr(citation, "matched_text", None))
        raw_text = str(raw_value).strip() if raw_value else str(citation).strip()
        normalized = _value_or_call(getattr(citation, "corrected_citation", None))
        normalized_text = str(normalized).strip() if normalized else None

        snippet = None
        if raw_text:
            idx = lower_text.find(raw_text.lower(), search_cursor)
            if idx == -1:
                idx = lower_text.find(raw_text.lower())
            if idx != -1:
                search_cursor = idx + len(raw_text)
                snippet = _build_snippet(text, idx, idx + len(raw_text))

        results.append(
            CitationResult(
                raw_text=raw_text or "(unavailable)",
                citation_type=type(citation).__name__,
                normalized_text=normalized_text,
                snippet=snippet,
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


async def collect_sources(
    pasted_text: str | None,
    uploaded_files: list[UploadFile] | None,
) -> tuple[list[SourceInput], list[str], str | None]:
    sources: list[SourceInput] = []
    warnings: list[str] = []

    text = (pasted_text or "").strip()
    valid_files = [file for file in (uploaded_files or []) if file and file.filename]

    if text:
        if valid_files:
            warnings.append("Both text and files were submitted. Text input was used.")
        sources.append(SourceInput(source_type="text", source_name=None, text=text, warnings=[]))
        return sources, warnings, None

    if not valid_files:
        return [], warnings, "Please provide pasted text or upload a .docx/.pdf file."

    for file in valid_files:
        extension = Path(file.filename or "").suffix.lower()
        if extension not in {".docx", ".pdf"}:
            warnings.append(f"Unsupported file skipped: {file.filename}")
            continue

        file_bytes = await file.read()
        try:
            if extension == ".docx":
                extracted_text = extract_text_from_docx(file_bytes)
                source_type = "docx"
            else:
                extracted_text = extract_text_from_pdf(file_bytes)
                source_type = "pdf"
        except Exception:
            warnings.append(f"Failed to parse file: {file.filename}")
            continue

        sources.append(
            SourceInput(
                source_type=source_type,
                source_name=file.filename,
                text=extracted_text,
                warnings=[],
            )
        )

    if not sources:
        return [], warnings, "No valid .docx or .pdf files were available to audit."

    return sources, warnings, None
