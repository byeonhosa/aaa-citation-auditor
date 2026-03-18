"""Generate a professional Citation Verification Report as a PDF.

Uses reportlab's Platypus framework for clean, print-ready layout.
The report is suitable for attaching to a case file or presenting to
a malpractice insurer as evidence of due diligence.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from aaa_db.models import AuditRun
from app.services.provenance import get_provenance

# ── Colour palette ────────────────────────────────────────────────────────────

_NAVY = colors.HexColor("#1a3557")
_ACCENT = colors.HexColor("#2563eb")
_GREEN_BG = colors.HexColor("#dcfce7")
_GREEN_FG = colors.HexColor("#15803d")
_AMBER_BG = colors.HexColor("#fef3c7")
_AMBER_FG = colors.HexColor("#b45309")
_RED_BG = colors.HexColor("#fee2e2")
_RED_FG = colors.HexColor("#b91c1c")
_BLUE_BG = colors.HexColor("#eff6ff")
_BLUE_FG = colors.HexColor("#1d4ed8")
_TEAL_BG = colors.HexColor("#ccfbf1")
_TEAL_FG = colors.HexColor("#0f766e")
_LIGHT_GRAY = colors.HexColor("#f8fafc")
_BORDER = colors.HexColor("#e2e8f0")
_TEXT = colors.HexColor("#1e293b")
_TEXT_2 = colors.HexColor("#475569")

# Status → (bg, fg, display label)
_STATUS_STYLE: dict[str, tuple[Any, Any, str]] = {
    "VERIFIED": (_GREEN_BG, _GREEN_FG, "Verified"),
    "STATUTE_VERIFIED": (_GREEN_BG, _GREEN_FG, "Statute Verified"),
    "DERIVED": (_BLUE_BG, _BLUE_FG, "Derived"),
    "STATUTE_DETECTED": (_TEAL_BG, _TEAL_FG, "Statute Detected"),
    "AMBIGUOUS": (_AMBER_BG, _AMBER_FG, "Ambiguous"),
    "NOT_FOUND": (_RED_BG, _RED_FG, "Not Found"),
    "ERROR": (_RED_BG, _RED_FG, "Error"),
    "UNVERIFIED_NO_TOKEN": (_LIGHT_GRAY, _TEXT_2, "Unverified"),
}

# Sort order for citation table grouping
_STATUS_ORDER: dict[str, int] = {
    "VERIFIED": 0,
    "STATUTE_VERIFIED": 1,
    "DERIVED": 2,
    "STATUTE_DETECTED": 3,
    "AMBIGUOUS": 4,
    "UNVERIFIED_NO_TOKEN": 5,
    "NOT_FOUND": 6,
    "ERROR": 7,
}


# ── Style sheet ───────────────────────────────────────────────────────────────


def _make_styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    normal = base["Normal"]
    return {
        "cover_title": ParagraphStyle(
            "cover_title",
            parent=normal,
            fontSize=22,
            leading=28,
            textColor=_NAVY,
            fontName="Helvetica-Bold",
            alignment=TA_CENTER,
            spaceAfter=6,
        ),
        "cover_sub": ParagraphStyle(
            "cover_sub",
            parent=normal,
            fontSize=11,
            leading=16,
            textColor=_TEXT_2,
            fontName="Helvetica",
            alignment=TA_CENTER,
            spaceAfter=4,
        ),
        "section_heading": ParagraphStyle(
            "section_heading",
            parent=normal,
            fontSize=13,
            leading=18,
            textColor=_NAVY,
            fontName="Helvetica-Bold",
            spaceBefore=14,
            spaceAfter=6,
        ),
        "body": ParagraphStyle(
            "body",
            parent=normal,
            fontSize=9.5,
            leading=14,
            textColor=_TEXT,
            fontName="Helvetica",
            spaceAfter=6,
        ),
        "body_small": ParagraphStyle(
            "body_small",
            parent=normal,
            fontSize=8.5,
            leading=12,
            textColor=_TEXT_2,
            fontName="Helvetica",
            spaceAfter=4,
        ),
        "disclaimer": ParagraphStyle(
            "disclaimer",
            parent=normal,
            fontSize=8.5,
            leading=13,
            textColor=_TEXT_2,
            fontName="Helvetica-Oblique",
            spaceAfter=6,
            spaceBefore=4,
        ),
        "table_header": ParagraphStyle(
            "table_header",
            parent=normal,
            fontSize=8,
            leading=10,
            textColor=colors.white,
            fontName="Helvetica-Bold",
            alignment=TA_LEFT,
        ),
        "table_cell": ParagraphStyle(
            "table_cell",
            parent=normal,
            fontSize=8,
            leading=11,
            textColor=_TEXT,
            fontName="Helvetica",
            wordWrap="LTR",
        ),
        "table_cell_center": ParagraphStyle(
            "table_cell_center",
            parent=normal,
            fontSize=8,
            leading=11,
            textColor=_TEXT,
            fontName="Helvetica",
            alignment=TA_CENTER,
        ),
        "risk_low": ParagraphStyle(
            "risk_low",
            parent=normal,
            fontSize=11,
            leading=16,
            textColor=_GREEN_FG,
            fontName="Helvetica-Bold",
        ),
        "risk_medium": ParagraphStyle(
            "risk_medium",
            parent=normal,
            fontSize=11,
            leading=16,
            textColor=_AMBER_FG,
            fontName="Helvetica-Bold",
        ),
        "risk_high": ParagraphStyle(
            "risk_high",
            parent=normal,
            fontSize=11,
            leading=16,
            textColor=_RED_FG,
            fontName="Helvetica-Bold",
        ),
        "footer": ParagraphStyle(
            "footer",
            parent=normal,
            fontSize=7.5,
            leading=10,
            textColor=_TEXT_2,
            fontName="Helvetica",
            alignment=TA_CENTER,
        ),
        "right": ParagraphStyle(
            "right",
            parent=normal,
            fontSize=9,
            leading=13,
            textColor=_TEXT_2,
            fontName="Helvetica",
            alignment=TA_RIGHT,
        ),
    }


# ── Risk assessment ───────────────────────────────────────────────────────────

_RISKY_STATUSES = frozenset({"NOT_FOUND", "AMBIGUOUS", "ERROR", "RATE_LIMITED"})


def _risk_level(run: AuditRun) -> tuple[str, str, int, int, int, str | None]:
    """Return (level, description, effectively_verified, genuinely_unverified, derived_vp,
    duplicate_note).

    Uses the same logic as the AI memo: DERIVED citations whose parent citation
    is VERIFIED are counted as effectively verified, not as risky.  Plain
    NOT_FOUND / AMBIGUOUS / ERROR citations are genuinely unverified.
    STATUTE_DETECTED citations are informational and do not affect risk.
    Unverified citations are deduplicated by normalized text before risk calculation.
    """
    total = run.citation_count
    if total == 0:
        return ("LOW", "No citations were found in this document.", 0, 0, 0, None)

    # Build a lookup of raw_text → status for non-DERIVED citations
    parent_status: dict[str, str] = {
        c.raw_text: (c.verification_status or "")
        for c in run.citations
        if c.raw_text and c.verification_status != "DERIVED"
    }

    derived_verified_parent = 0
    derived_risky_parent = 0
    for c in run.citations:
        if c.verification_status != "DERIVED":
            continue
        pstatus = parent_status.get(c.resolved_from or "", "") if c.resolved_from else ""
        if pstatus == "VERIFIED":
            derived_verified_parent += 1
        elif pstatus in _RISKY_STATUSES or not pstatus:
            derived_risky_parent += 1

    effectively_verified = (
        (run.verified_count or 0) + (run.statute_verified_count or 0) + derived_verified_parent
    )

    # Deduplicate risky citations by normalized_text (or raw_text) so repeated
    # occurrences of the same unverified citation don't inflate the risk score.
    _seen_risky: set[str] = set()
    for c in run.citations:
        if (c.verification_status or "") in _RISKY_STATUSES:
            key = (getattr(c, "normalized_text", None) or c.raw_text or "").strip()
            if key:
                _seen_risky.add(key)
    unique_risky_from_status = len(_seen_risky)
    raw_risky_count = (
        (run.not_found_count or 0) + (run.ambiguous_count or 0) + (run.error_count or 0)
    )
    # If no citations were loaded (e.g. lazy-load skipped), fall back to aggregate counts
    if unique_risky_from_status == 0 and not run.citations:
        unique_risky_from_status = raw_risky_count
    dup_count = raw_risky_count - unique_risky_from_status
    duplicate_note: str | None = (
        f"Note: {dup_count} citation(s) appear multiple times; "
        f"risk assessment is based on {unique_risky_from_status} unique unverified citation(s)."
        if dup_count > 0
        else None
    )

    genuinely_unverified = unique_risky_from_status + derived_risky_parent
    statute_detected = run.statute_count or 0

    statute_note = (
        f" {statute_detected} statute citation(s) detected but not fully verified (informational)."
        if statute_detected
        else ""
    )

    if genuinely_unverified == 0:
        return (
            "LOW",
            f"All {effectively_verified} of {total} citations are effectively verified"
            + (
                f" (including {derived_verified_parent} derived citation(s) with verified parents)"
                if derived_verified_parent
                else ""
            )
            + f" with no unresolved items.{statute_note}",
            effectively_verified,
            genuinely_unverified,
            derived_verified_parent,
            duplicate_note,
        )

    ratio = genuinely_unverified / total
    if ratio < 0.05:
        return (
            "LOW",
            f"{effectively_verified} of {total} citations are effectively verified"
            + (
                f" (including {derived_verified_parent} derived citation(s) with verified parents)"
                if derived_verified_parent
                else ""
            )
            + f". {genuinely_unverified} citation(s) could not be verified.{statute_note}",
            effectively_verified,
            genuinely_unverified,
            derived_verified_parent,
            duplicate_note,
        )
    if ratio <= 0.15:
        return (
            "MEDIUM",
            f"{effectively_verified} of {total} citations are effectively verified. "
            f"{genuinely_unverified} citation(s) require attention.{statute_note}",
            effectively_verified,
            genuinely_unverified,
            derived_verified_parent,
            duplicate_note,
        )
    return (
        "HIGH",
        f"{genuinely_unverified} of {total} citations could not be verified or are unresolved. "
        f"Manual review is strongly recommended before filing.{statute_note}",
        effectively_verified,
        genuinely_unverified,
        derived_verified_parent,
        duplicate_note,
    )


# ── Integrity fingerprint ─────────────────────────────────────────────────────


def _report_fingerprint(run: AuditRun) -> str:
    """Return a SHA-256 fingerprint of the run's citation data.

    This is not a hash of the original document (the full text is never
    stored).  It is a deterministic fingerprint of the verification data
    recorded in this report, suitable for integrity verification.
    """
    parts = [
        f"run_id={run.id}",
        f"created_at={run.created_at.isoformat() if run.created_at else ''}",
        f"source_type={run.source_type}",
        f"source_name={run.source_name or ''}",
        f"citation_count={run.citation_count}",
    ]
    for c in sorted(run.citations, key=lambda x: x.id):
        parts.append(f"{c.raw_text}|{c.verification_status or ''}|{c.resolution_method or ''}")
    digest = hashlib.sha256("\n".join(parts).encode()).hexdigest()
    # Format as groups of 8 for readability
    return " ".join(digest[i : i + 8] for i in range(0, 32, 8))


# ── Page template (header/footer on every page) ───────────────────────────────


class _PageTemplate:
    """Callable for SimpleDocTemplate's onPage / onLaterPages hooks."""

    def __init__(self, generated_dt: str, styles: dict[str, ParagraphStyle]) -> None:
        self._dt = generated_dt
        self._footer_style = styles["footer"]

    def __call__(self, canvas, doc) -> None:
        canvas.saveState()
        w, h = letter
        margin = 0.65 * inch

        # Top rule
        canvas.setStrokeColor(_BORDER)
        canvas.setLineWidth(0.5)
        canvas.line(margin, h - 0.5 * inch, w - margin, h - 0.5 * inch)

        # Bottom rule + footer text
        canvas.line(margin, 0.55 * inch, w - margin, 0.55 * inch)
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(_TEXT_2)
        canvas.drawCentredString(
            w / 2,
            0.35 * inch,
            f"FinalVerify Citation Verification Report  —  Page {doc.page}  —  "
            f"Generated: {self._dt}",
        )
        canvas.restoreState()


# ── Public API ────────────────────────────────────────────────────────────────


def generate_pdf_report(
    run: AuditRun,
    *,
    user_run_number: int = 0,
    user_email: str = "",
) -> bytes:
    """Generate a professional Citation Verification Report as PDF bytes.

    Parameters
    ----------
    run:
        The :class:`~aaa_db.models.AuditRun` to report on.  The
        ``citations`` relationship must already be loaded.
    user_run_number:
        The per-user sequential run number (display only).
    user_email:
        Email of the user who owns the run (display only).
    """
    audit_mode = getattr(run, "audit_mode", "self_review") or "self_review"
    is_opposing = audit_mode == "opposing_review"

    buf = BytesIO()
    now = datetime.now(tz=timezone.utc)
    generated_dt = now.strftime("%Y-%m-%d %H:%M UTC")
    styles = _make_styles()
    page_hook = _PageTemplate(generated_dt, styles)

    report_title = (
        "Opposing Filing Citation Analysis Report"
        if is_opposing
        else "Citation Verification Report"
    )

    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        title=report_title,
        author="FinalVerify",
        subject=f"{report_title} — Run #{user_run_number or run.id}",
    )

    story: list[Any] = []

    # ── Cover / Header ────────────────────────────────────────────────────────
    story.append(Spacer(1, 0.15 * inch))
    story.append(Paragraph(report_title, styles["cover_title"]))
    story.append(Paragraph("Prepared by FinalVerify — finalverify.com", styles["cover_sub"]))
    if is_opposing:
        story.append(
            Paragraph(
                "Analysis of opposing party's filing for citation weaknesses and vulnerabilities.",
                styles["cover_sub"],
            )
        )
    story.append(Spacer(1, 0.1 * inch))

    source_label = run.source_name or "(pasted text)"
    meta_rows = [
        ("Date of report:", generated_dt),
        (
            "Filing analyzed:" if is_opposing else "Document audited:",
            f"{source_label} ({run.source_type})",
        ),
        ("Report ID:", f"Run #{user_run_number or run.id}"),
        ("Mode:", "Opposing Counsel Review" if is_opposing else "Self Review"),
        ("Total citations:", str(run.citation_count)),
    ]
    meta_table = Table(
        meta_rows,
        colWidths=[1.6 * inch, None],
        hAlign="LEFT",
    )
    meta_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 9.5),
                ("TEXTCOLOR", (0, 0), (0, -1), _NAVY),
                ("TEXTCOLOR", (1, 0), (1, -1), _TEXT),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(meta_table)
    story.append(HRFlowable(width="100%", thickness=1, color=_NAVY, spaceAfter=10))

    # ── Executive Summary ─────────────────────────────────────────────────────
    story.append(
        Paragraph(
            "Opposing Filing Analysis Summary" if is_opposing else "Executive Summary",
            styles["section_heading"],
        )
    )
    if is_opposing:
        story.append(
            Paragraph(
                "This report analyzes the opposing party's filing for citation weaknesses. "
                "Citations that could not be verified, are ambiguous, or are unresolved may "
                "represent vulnerabilities in their argument.",
                styles["body"],
            )
        )

    risk, risk_desc, _eff_verified, _genuinely_unverified, _derived_vp, dup_note = _risk_level(run)
    risk_label = {"LOW": "Low Risk", "MEDIUM": "Medium Risk", "HIGH": "High Risk"}[risk]
    risk_prefix = (
        "<b>Citation Vulnerability Assessment:</b>"
        if is_opposing
        else "<b>Overall Risk Assessment:</b>"
    )
    story.append(
        Paragraph(
            f"{risk_prefix} <font color='#{_get_hex(risk, 'fg')}'>"
            f"{risk_label}</font>  —  {risk_desc}",
            styles["body"],
        )
    )
    if dup_note:
        story.append(Paragraph(_esc(dup_note), styles["body_small"]))
    story.append(Spacer(1, 6))

    # Summary metrics table
    sv = run.statute_verified_count or 0
    summary_data = [
        ["Metric", "Count"],
        ["Total citations analyzed", str(run.citation_count)],
        ["Verified (case law)", str(run.verified_count or 0)],
        ["Statutes verified", str(sv)],
        ["Derived (Id./short-form)", str(run.derived_count or 0)],
        ["Statute detected (unverified)", str(run.statute_count or 0)],
        ["Ambiguous — multiple matches", str(run.ambiguous_count or 0)],
        ["Not found", str(run.not_found_count or 0)],
        ["Error during verification", str(run.error_count or 0)],
        ["Unverified — no API token", str(run.unverified_no_token_count or 0)],
    ]
    summary_table = Table(
        summary_data,
        colWidths=[3.5 * inch, 1.2 * inch],
        hAlign="LEFT",
    )
    summary_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), _NAVY),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("ALIGN", (1, 0), (1, -1), "CENTER"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _LIGHT_GRAY]),
                ("GRID", (0, 0), (-1, -1), 0.5, _BORDER),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(summary_table)

    # ── Provenance Summary ────────────────────────────────────────────────────
    story.append(Paragraph("Provenance Summary", styles["section_heading"]))
    story.append(
        Paragraph(
            "The following table shows how verified citations were confirmed. "
            "Provenance indicates the method and source used for each verification.",
            styles["body"],
        )
    )

    prov_counts: dict[str, int] = {}
    for c in run.citations:
        info = get_provenance(c.verification_status, c.resolution_method)
        prov_counts[info.label] = prov_counts.get(info.label, 0) + 1

    if prov_counts:
        prov_rows = [["Verification Method", "Count"]] + [
            [label, str(count)] for label, count in sorted(prov_counts.items(), key=lambda x: -x[1])
        ]
        prov_table = Table(prov_rows, colWidths=[3.5 * inch, 1.2 * inch], hAlign="LEFT")
        prov_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), _NAVY),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("ALIGN", (1, 0), (1, -1), "CENTER"),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _LIGHT_GRAY]),
                    ("GRID", (0, 0), (-1, -1), 0.5, _BORDER),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ]
            )
        )
        story.append(prov_table)
    else:
        story.append(Paragraph("No citations to summarize.", styles["body_small"]))

    # ── Citation Details ──────────────────────────────────────────────────────
    story.append(PageBreak())
    story.append(Paragraph("Citation Details", styles["section_heading"]))
    if is_opposing:
        story.append(
            Paragraph(
                "All citations from the opposing filing are listed below, grouped by status. "
                "Red rows indicate citations that could not be verified and may represent "
                "argument vulnerabilities; amber rows indicate citations requiring attention; "
                "green rows indicate confirmed citations.",
                styles["body"],
            )
        )
    else:
        story.append(
            Paragraph(
                "All citations are listed below, grouped by status. "
                "Green rows indicate verified citations; amber indicates citations requiring "
                "review; red indicates citations that could not be verified.",
                styles["body"],
            )
        )
    story.append(Spacer(1, 6))

    if run.citations:
        sorted_citations = sorted(
            run.citations,
            key=lambda c: _STATUS_ORDER.get(c.verification_status or "", 99),
        )

        header = [
            Paragraph("#", styles["table_header"]),
            Paragraph("Citation", styles["table_header"]),
            Paragraph("Status", styles["table_header"]),
            Paragraph("Provenance", styles["table_header"]),
            Paragraph("Detail / Case Name", styles["table_header"]),
            Paragraph("Snippet", styles["table_header"]),
        ]
        rows = [header]
        row_styles: list[tuple[Any, ...]] = []

        for seq, c in enumerate(sorted_citations, start=1):
            status = c.verification_status or "UNKNOWN"
            bg, _fg, display_status = _STATUS_STYLE.get(status, (_LIGHT_GRAY, _TEXT_2, status))
            info = get_provenance(c.verification_status, c.resolution_method)
            snippet = (c.snippet or "")[:100] + ("…" if len(c.snippet or "") > 100 else "")
            detail = _clean_detail_for_report(c.verification_detail or "")

            rows.append(
                [
                    Paragraph(str(seq), styles["table_cell_center"]),
                    Paragraph(_esc(c.raw_text or ""), styles["table_cell"]),
                    Paragraph(_esc(display_status), styles["table_cell"]),
                    Paragraph(_esc(info.label), styles["table_cell"]),
                    Paragraph(_esc(detail), styles["table_cell"]),
                    Paragraph(_esc(snippet), styles["table_cell"]),
                ]
            )
            data_row = seq  # 1-based; table row index = seq (header is row 0)
            row_styles.append(("BACKGROUND", (0, data_row), (-1, data_row), bg))

        col_widths = [
            0.35 * inch,
            1.9 * inch,
            0.9 * inch,
            1.0 * inch,
            1.25 * inch,
            1.3 * inch,
        ]
        details_table = Table(rows, colWidths=col_widths, repeatRows=1)
        base_style = [
            ("BACKGROUND", (0, 0), (-1, 0), _NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ("GRID", (0, 0), (-1, -1), 0.3, _BORDER),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ]
        details_table.setStyle(TableStyle(base_style + row_styles))
        story.append(details_table)
    else:
        story.append(Paragraph("No citations were recorded for this run.", styles["body_small"]))

    # ── Scope and Limitations ─────────────────────────────────────────────────
    story.append(PageBreak())
    story.append(Paragraph("Scope and Limitations", styles["section_heading"]))
    story.append(HRFlowable(width="100%", thickness=0.5, color=_BORDER, spaceAfter=8))
    story.append(
        Paragraph(
            "This report verifies citation existence and identity against open legal databases "
            "including CourtListener, the Virginia Code, and the U.S. Code. It does not "
            "constitute a complete citator analysis (such as KeyCite or Shepard\u2019s), does not "
            "verify whether cited authorities remain good law, and does not verify the accuracy "
            "of propositions attributed to cited cases. This report is provided as a "
            "quality-assurance tool and does not constitute legal advice.",
            styles["disclaimer"],
        )
    )

    # ── Certification Statement ───────────────────────────────────────────────
    story.append(Paragraph("Certification Statement", styles["section_heading"]))
    story.append(HRFlowable(width="100%", thickness=0.5, color=_BORDER, spaceAfter=8))
    cert_body = (
        f"This Opposing Filing Citation Analysis Report was generated on {generated_dt} by "
        f"FinalVerify (finalverify.com) to analyze the filing identified above for citation "
        f"weaknesses. The verification was performed using the sources and methods described in "
        f"this report."
        if is_opposing
        else f"This Citation Verification Report was generated on {generated_dt} by FinalVerify "
        f"(finalverify.com) for the document identified above. The verification was performed "
        f"using the sources and methods described in this report."
    )
    story.append(Paragraph(cert_body, styles["body"]))

    fingerprint = _report_fingerprint(run)
    cert_rows = [
        ("Report ID:", f"Run #{user_run_number or run.id}"),
        ("Generated:", generated_dt),
        (
            "Verification data fingerprint:",
            fingerprint,
        ),
        (
            "Fingerprint note:",
            "SHA-256 of the citation verification data recorded in this report. "
            "The original document content is never stored.",
        ),
    ]
    cert_table = Table(cert_rows, colWidths=[1.9 * inch, None], hAlign="LEFT")
    cert_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("TEXTCOLOR", (0, 0), (0, -1), _NAVY),
                ("TEXTCOLOR", (1, 0), (1, -1), _TEXT),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(cert_table)

    # ── Build ─────────────────────────────────────────────────────────────────
    doc.build(story, onFirstPage=page_hook, onLaterPages=page_hook)
    return buf.getvalue()


# ── Helpers ───────────────────────────────────────────────────────────────────

# Matches parenthetical cluster references: (cluster NNN), (clusters: N),
# (... cluster NNN ...) — i.e. any parenthetical containing a cluster reference.
_CLUSTER_PAT = re.compile(
    r"\s*\((?:clusters?:?\s*\d+|[^)]*,?\s*cluster\s+\d+[^)]*)\)",
    re.IGNORECASE,
)

# Detail prefixes added by the verification pipeline that are not meaningful
# to end-users; we strip these and keep only the case name that follows.
_DETAIL_PREFIXES = (
    "Matched in local citation index",
    "Resolved from cache",
    "Auto-resolved by heuristic",
    "Resolved automatically (duplicate candidates removed)",
    "Resolved automatically",
    "CourtListener matched citation",
)


def _clean_detail_for_report(detail: str) -> str:
    """Strip internal references (cluster IDs, DB prefixes) from verification detail.

    For PDF output only — keeps cluster IDs intact in the web UI.
    Returns the case name when available, otherwise a cleaned short description.
    """
    if not detail:
        return ""
    # Remove cluster ID parentheticals
    cleaned = _CLUSTER_PAT.sub("", detail).strip().rstrip(".")
    # For "Prefix. Case Name." style details, return just the case name
    for prefix in _DETAIL_PREFIXES:
        if cleaned.lower().startswith(prefix.lower()):
            rest = cleaned[len(prefix) :].lstrip(". ")
            return rest.rstrip(".") if rest else ""
    return cleaned[:80]


def _esc(text: str) -> str:
    """Escape XML special characters for ReportLab Paragraphs."""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _get_hex(risk: str, part: str) -> str:
    """Return a hex colour string for risk level labels."""
    mapping = {
        "LOW": {"fg": "15803d"},
        "MEDIUM": {"fg": "b45309"},
        "HIGH": {"fg": "b91c1c"},
    }
    return mapping.get(risk, {}).get(part, "1e293b")
