from __future__ import annotations

import csv
from io import StringIO
from typing import Any

from aaa_db.models import AuditRun


def _source_label(run: AuditRun) -> str:
    if run.source_name:
        return f"{run.source_name} ({run.source_type})"
    return run.source_type


def _citation_rows(run: AuditRun) -> list[dict[str, str]]:
    source = _source_label(run)
    rows: list[dict[str, str]] = []
    for citation in run.citations:
        rows.append(
            {
                "source": source,
                "raw_text": citation.raw_text,
                "citation_type": citation.citation_type,
                "normalized_text": citation.normalized_text or "",
                "resolved_from": citation.resolved_from or "",
                "verification_status": citation.verification_status or "",
                "verification_detail": citation.verification_detail or "",
                "snippet": citation.snippet or "",
                "resolution_method": citation.resolution_method or "",
            }
        )
    return rows


def export_markdown_for_run(run: AuditRun) -> str:
    lines = [f"# AAA Export - Audit Run #{run.id}", ""]
    lines.append(f"Source: {_source_label(run)}")
    lines.append(f"Citations: {run.citation_count}")
    lines.append(
        f"Summary: VERIFIED={run.verified_count}, NOT_FOUND={run.not_found_count}, "
        f"AMBIGUOUS={run.ambiguous_count}, DERIVED={run.derived_count}, "
        f"STATUTE_DETECTED={run.statute_count}, "
        f"STATUTE_VERIFIED={run.statute_verified_count or 0}, "
        f"ERROR={run.error_count}, UNVERIFIED_NO_TOKEN={run.unverified_no_token_count}"
    )
    lines.append("")

    for index, row in enumerate(_citation_rows(run), start=1):
        lines.extend(
            [
                f"## Citation {index}",
                f"- Source: {row['source']}",
                f"- Raw text: {row['raw_text']}",
                f"- Type: {row['citation_type']}",
                f"- Normalized: {row['normalized_text'] or '(not available)'}",
                f"- Resolved from: {row['resolved_from'] or '(unresolved)'}",
                f"- Verification: {row['verification_status'] or 'UNKNOWN'}",
                f"- Verification detail: {row['verification_detail'] or '(none)'}",
                f"- Snippet: {row['snippet'] or '(snippet unavailable)'}",
                "",
            ]
        )

    if run.citation_count == 0:
        lines.append("No citations were found for this run.")

    return "\n".join(lines)


def export_csv_for_run(run: AuditRun) -> str:
    buffer = StringIO()
    fieldnames = [
        "source",
        "raw_text",
        "citation_type",
        "normalized_text",
        "resolved_from",
        "verification_status",
        "verification_detail",
        "snippet",
        "resolution_method",
    ]
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for row in _citation_rows(run):
        writer.writerow(row)
    return buffer.getvalue()


def export_print_html_context(run: AuditRun) -> dict[str, Any]:
    return {
        "title": f"Printable Export - Run #{run.id}",
        "run": {
            "id": run.id,
            "source_type": run.source_type,
            "source_name": run.source_name,
            "citation_count": run.citation_count,
            "created_at": run.created_at,
        },
        "rows": _citation_rows(run),
    }
