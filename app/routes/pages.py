from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.services.audit import extract_citations, extract_source_text, resolve_id_citations
from app.services.verification import summarize_verification_statuses, verify_citations
from app.settings import TEMPLATES_DIR, settings

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

PASTED_TEXT_FORM = Form(default="")
UPLOADED_FILE_FORM = File(default=None)


def citation_to_context(citation) -> dict[str, str | None]:
    return {
        "raw_text": citation.raw_text,
        "citation_type": citation.citation_type,
        "normalized_text": citation.normalized_text,
        "resolved_from": citation.resolved_from,
        "verification_status": citation.verification_status,
        "verification_detail": citation.verification_detail,
    }


def render_dashboard(
    request: Request,
    *,
    pasted_text: str = "",
    citations: list[dict[str, str | None]] | None = None,
    source_type: str | None = None,
    warnings: list[str] | None = None,
    validation_message: str | None = None,
    verification_summary: dict[str, int] | None = None,
) -> HTMLResponse:
    citations = citations or []
    warnings = warnings or []
    verification_summary = verification_summary or {}

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "title": "Audit Dashboard",
            "pasted_text": pasted_text,
            "citations": citations,
            "source_type": source_type,
            "warning_messages": warnings,
            "validation_message": validation_message,
            "citation_count": len(citations),
            "verification_summary": verification_summary,
        },
    )


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    return render_dashboard(request)


@router.post("/audit", response_class=HTMLResponse)
async def run_audit(
    request: Request,
    pasted_text: str = PASTED_TEXT_FORM,
    uploaded_file: UploadFile | None = UPLOADED_FILE_FORM,
) -> HTMLResponse:
    text, source_type, warnings, validation_message = await extract_source_text(
        pasted_text, uploaded_file
    )

    if validation_message:
        return render_dashboard(
            request,
            pasted_text=pasted_text,
            warnings=warnings,
            validation_message=validation_message,
        )

    citation_results, parsing_warnings = extract_citations(text or "")
    citation_results = resolve_id_citations(citation_results)
    citation_results = verify_citations(
        citation_results,
        courtlistener_token=settings.courtlistener_token,
        verification_base_url=settings.verification_base_url,
        verification_timeout_seconds=settings.verification_timeout_seconds,
    )
    warnings.extend(parsing_warnings)
    verification_summary = summarize_verification_statuses(citation_results)

    return render_dashboard(
        request,
        pasted_text=pasted_text,
        citations=[citation_to_context(citation) for citation in citation_results],
        source_type=source_type,
        warnings=warnings,
        verification_summary=verification_summary,
    )


@router.get("/history", response_class=HTMLResponse)
def history(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="history.html",
        context={"title": "History"},
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={"title": "Settings"},
    )
