from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

import fitz
from docx import Document
from eyecite import get_citations
from fastapi import UploadFile

logger = logging.getLogger(__name__)

# ── Supplemental statute patterns (eyecite does not cover these) ─────────────
#
# Eyecite is a case-law citation parser.  It handles U.S.C. and a handful of
# state codes, but misses many common statute formats.  The patterns below are
# applied as a supplemental pass *after* eyecite extraction and are deduplicated
# against eyecite results by position so no citation is counted twice.
#
# To add support for a new state, append a (label, compiled_regex) tuple to
# _SUPPLEMENTAL_STATUTE_PATTERNS.  Each pattern must include the section
# symbol and number so the full citation text is captured.
#
# Pattern 1 — dotted-abbreviation formats
#   "20-A M.R.S. § 1001(20)"  (Maine)
#   "16 V.S.A. § 833"          (Vermont)
#   "R.S.Mo. § 162.670"        (Missouri)
#   "Rev. Stat. § 123"
_DOTTED_ABBREV_RE = re.compile(
    r"""
    (?:\b\d+(?:-[A-Z])?\s+)?          # optional numeric title prefix: "20-A " / "16 "
    (?:
        [A-Z][A-Za-z]{0,4}            # first abbreviation segment (M, V, R, …)
        \.                            # dot separator
        (?:[A-Za-z]{0,5}\.)+          # one or more further dotted segments (R.S., S., Mo., …)
        |
        (?:Rev|Gen|Comp|Ann)\.        # descriptive keyword
        \s+(?:Stat|Code)\.            # "Stat." or "Code."
        (?:\s+Ann\.)?                 # optional "Ann." suffix
    )
    \s*(?:§|Sec\.|Section)\s*         # section indicator: § or Sec. or Section
    \d[\d.()\-]*                      # section number (e.g., 1001, 162.670, 1001(20))
    """,
    re.VERBOSE | re.UNICODE | re.IGNORECASE,
)

# Pattern 2 — Virginia Code named-code formats (eyecite misses these entirely)
#   "Va. Code § 15.2-3400"       "Va. Code Section 15.2-3400"
#   "Va. Code Ann. § 15.2-3400"  "Va. Code Sec. 15.2-3400"
#   "Code of Virginia § 18.2-308"
#   "Code of Va. § 46.2-100"
#   "Virginia Code § 15.2-3400"
#   "Code of Virginia, 1950, as amended, § 15.2-1300"
_VA_CODE_RE = re.compile(
    r"""
    (?:
        (?:Va\.?\s+|Virginia\s+)Code(?:\.?\s+Ann\.?)?  # Va. Code [Ann.] | Virginia Code
        | Code\s+of\s+(?:Va\.?|Virginia)               # Code of Virginia | Code of Va.
          (?:[^§\n]{0,50})?                             # optional year / parenthetical
    )
    \s*,?\s*                                            # optional comma + whitespace
    (?:§|Sec\.|Section)\s*                              # section indicator: § or Sec. or Section
    \d+(?:\.\d+)?                                       # title: 1 | 15.2 | 46.2
    [-\N{EN DASH}]                                      # hyphen or en-dash
    \d[\dA-Z]*(?:[.:\-]\d[\dA-Z]*)*                    # section number
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Pattern 3 — U.S. Code without periods and verbose "United States Code" form
#   "42 USC § 1983"                              (no-period abbreviation)
#   "Title 42, United States Code, Section 1983" (verbose form)
#
# NOTE: "42 U.S.C. § 1983" (dotted) is handled by both eyecite and
# _DOTTED_ABBREV_RE; this pattern catches only the forms those miss.
_USC_ALTERNATE_RE = re.compile(
    r"""
    (?:
        (?:Title\s+)?                        # optional "Title "
        \d+\s+                               # title number
        USC                                  # USC — no periods
        (?!\.)                               # not followed by period (avoids U.S.C.)
        (?:\s+Ann\.)?                        # optional Ann.
        |
        Title\s+\d+\s*,?\s*                  # "Title 42,"
        United\s+States\s+Code\s*,?\s*       # "United States Code,"
    )
    (?:§|Sec\.|Section)\s*                   # section indicator
    \d[\dA-Za-z]*(?:\([^)]*\))?             # section number + optional sub
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Central extensible list — add new state patterns here.
# Each entry is a (label, compiled_pattern) tuple.
_SUPPLEMENTAL_STATUTE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("dotted_abbrev", _DOTTED_ABBREV_RE),
    ("va_code", _VA_CODE_RE),
    ("usc_alternate", _USC_ALTERNATE_RE),
]


@dataclass
class CitationResult:
    raw_text: str
    citation_type: str
    normalized_text: str | None = None
    resolved_from: str | None = None
    verification_status: str | None = None
    verification_detail: str | None = None
    snippet: str | None = None
    candidate_cluster_ids: list[int] | None = None
    candidate_metadata: list[dict] | None = None
    selected_cluster_id: int | None = None
    resolution_method: str | None = None


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


def _spans_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """Return True if the two half-open character ranges [a, b) and [c, d) overlap."""
    return a_start < b_end and b_start < a_end


def _find_supplemental_statutes(
    text: str,
    existing_spans: list[tuple[int, int]],
    existing_results: list[CitationResult],
) -> list[CitationResult]:
    """Supplemental regex pass for statute formats eyecite does not extract.

    Uses *position-based* deduplication: a regex match is skipped when its
    character range overlaps with any span already claimed by an eyecite result
    (or a prior supplemental match in this call).  This prevents double-counting
    when both eyecite and a supplemental pattern cover the same text region.

    A secondary text-based check is also applied so that two different patterns
    matching the *same literal string at different positions* produce only one
    CitationResult per unique raw text (matching eyecite's own behaviour for
    repeated statute citations).
    """
    # Seed with existing FullLawCitation texts so we don't re-add what eyecite
    # already found by text string (belt-and-suspenders alongside position check).
    seen_texts: set[str] = {
        r.raw_text.strip().lower() for r in existing_results if r.citation_type == "FullLawCitation"
    }
    # Work on a *copy* of the spans list so we don't mutate the caller's list
    # while still tracking spans claimed by new matches within this call.
    occupied: list[tuple[int, int]] = list(existing_spans)

    extra: list[CitationResult] = []
    for _label, pattern in _SUPPLEMENTAL_STATUTE_PATTERNS:
        for m in pattern.finditer(text):
            matched = m.group(0).strip()
            key = matched.lower()

            # Text dedup — skip if this exact string is already known
            if key in seen_texts:
                continue

            # Position dedup — skip if this match overlaps an existing span
            m_start, m_end = m.start(), m.end()
            if any(_spans_overlap(m_start, m_end, s, e) for s, e in occupied):
                continue

            seen_texts.add(key)
            occupied.append((m_start, m_end))
            snippet = _build_snippet(text, m_start, m_end)
            extra.append(
                CitationResult(
                    raw_text=matched,
                    citation_type="FullLawCitation",
                    snippet=snippet,
                )
            )
            logger.debug("Supplemental statute detected (%s): %r", _label, matched)
    return extra


# ── Citation fragment filter ──────────────────────────────────────────────────
#
# eyecite occasionally extracts bare section symbols ("§", "§§") as
# UnknownCitation objects when it can't associate the symbol with a statute.
# These are not real citations and must be removed before verification to avoid:
#   • False CourtListener matches for bare "§"
#   • Id. citations deriving from a garbage parent
#   • Duplicate entries alongside the correctly-detected STATUTE_DETECTED hit
#
# Filter rules (conservative — when in doubt, keep):
#   1. Strip if raw_text has fewer than 3 characters after stripping whitespace.
#   2. Strip if raw_text contains only Unicode symbols / punctuation (no letters
#      or digits) — catches "§", "§§", "§§§", etc.
#   3. Strip UnknownCitation objects whose raw_text is only symbols/punctuation
#      (same test as rule 2, restricted to the Unknown type).
#
# All filtered citations are logged at DEBUG level so the filter is auditable.


def _has_alphanumeric(text: str) -> bool:
    """Return True if *text* contains at least one letter or digit."""
    return any(unicodedata.category(ch)[0] in {"L", "N"} for ch in text)


def _is_citation_fragment(citation: CitationResult) -> bool:
    """Return True if *citation* is an invalid fragment that should be dropped.

    Rules are conservative: only obviously-invalid extractions are rejected.
    """
    raw = citation.raw_text.strip()

    # Rule 1: too short to be a real citation
    if len(raw) < 3:
        return True

    # Rule 2: no letters or digits — pure symbol/punctuation (e.g. "§", "§§")
    if not _has_alphanumeric(raw):
        return True

    # Rule 3: UnknownCitation with only symbols — still pure noise even if ≥3 chars
    if citation.citation_type == "UnknownCitation" and not _has_alphanumeric(raw):
        return True

    return False


def filter_citation_fragments(
    citations: list[CitationResult],
) -> tuple[list[CitationResult], list[CitationResult]]:
    """Split *citations* into (valid, filtered) lists.

    Filtered citations are logged at DEBUG level for auditability.
    Returns a 2-tuple: (kept citations, dropped citations).
    """
    kept: list[CitationResult] = []
    dropped: list[CitationResult] = []
    for citation in citations:
        if _is_citation_fragment(citation):
            dropped.append(citation)
            logger.debug(
                "Citation fragment filtered out: type=%s raw=%r",
                citation.citation_type,
                citation.raw_text,
            )
        else:
            kept.append(citation)
    if dropped:
        logger.info("Filtered %d citation fragment(s) (kept %d)", len(dropped), len(kept))
    return kept, dropped


def extract_citations(text: str) -> tuple[list[CitationResult], list[str]]:
    warnings: list[str] = []
    results: list[CitationResult] = []

    if not text.strip():
        return results, ["No text was available to parse for citations."]

    search_cursor = 0
    lower_text = text.lower()
    # Track character spans of eyecite-extracted citations for position-based
    # deduplication in the supplemental statute pass below.
    eyecite_spans: list[tuple[int, int]] = []

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
                span_end = idx + len(raw_text)
                snippet = _build_snippet(text, idx, span_end)
                # Only claim a span for real citations (alphanumeric content).
                # Bare § symbols extracted by eyecite must not block supplemental
                # statute patterns whose match regions happen to contain that §.
                if _has_alphanumeric(raw_text):
                    eyecite_spans.append((idx, span_end))

        results.append(
            CitationResult(
                raw_text=raw_text or "(unavailable)",
                citation_type=type(citation).__name__,
                normalized_text=normalized_text,
                snippet=snippet,
            )
        )

    # Supplemental pass: catch statute formats eyecite does not extract
    # (e.g. "Va. Code § 15.2-3400", "20-A M.R.S. § 1001").
    # Position-based deduplication prevents double-counting with eyecite results.
    extra = _find_supplemental_statutes(text, eyecite_spans, results)
    if extra:
        logger.info("Supplemental statute extraction: found %d additional statute(s)", len(extra))
        results.extend(extra)

    # Filter pass: remove bare section symbols and other fragments eyecite
    # over-parses.  This runs after the state-statute pass so that any valid
    # statute citations added above are NOT filtered out (they always have
    # alphanumeric content in their raw_text).
    results, _ = filter_citation_fragments(results)

    if not results:
        warnings.append("No citations were detected.")

    return results, warnings


def validate_upload_limits(
    files: list[UploadFile],
    max_files: int,
    max_file_size_mb: int,
) -> str | None:
    if len(files) > max_files:
        return f"Too many files uploaded. The limit is {max_files} file(s) per batch."
    max_bytes = max_file_size_mb * 1024 * 1024
    for file in files:
        if file.size is not None and file.size > max_bytes:
            size_mb = file.size / (1024 * 1024)
            return (
                f'"{file.filename}" is {size_mb:.1f} MB, which exceeds the '
                f"{max_file_size_mb} MB file size limit."
            )
    return None


def apply_citation_cap(
    citations: list[CitationResult],
    limit: int,
) -> tuple[list[CitationResult], str | None]:
    if len(citations) <= limit:
        return citations, None
    warning = (
        f"This document contains {len(citations)} citations. "
        f"Only the first {limit} were processed. "
        "Consider splitting the document into smaller sections."
    )
    return citations[:limit], warning


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
    *,
    max_files: int = 10,
    max_file_size_mb: int = 50,
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
        return [], warnings, "Please enter text or upload a document to audit."

    error = validate_upload_limits(valid_files, max_files, max_file_size_mb)
    if error:
        return [], warnings, error

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
