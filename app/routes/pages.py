from contextlib import contextmanager
from time import perf_counter
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from aaa_db.models import AuditRun
from aaa_db.repository import get_audit_run, list_audit_runs, save_audit_run
from aaa_db.session import SessionLocal
from aaa_db.telemetry_repository import get_or_create_install_id, record_telemetry_event
from app.services.audit import collect_sources, extract_citations, resolve_id_citations
from app.services.verification import summarize_verification_statuses, verify_citations
from app.settings import TEMPLATES_DIR, settings

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

PASTED_TEXT_FORM = Form(default="")
UPLOADED_FILES_FORM = File(default=None)


@contextmanager
def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _record_event_safely(**kwargs) -> None:  # noqa: ANN003
    try:
        install_id = get_or_create_install_id()
        with db_session() as db:
            record_telemetry_event(
                db,
                install_id=install_id,
                app_version=settings.app_version,
                **kwargs,
            )
    except Exception:
        pass


def citation_to_context(citation: Any) -> dict[str, str | None]:
    return {
        "raw_text": citation.raw_text,
        "citation_type": citation.citation_type,
        "normalized_text": citation.normalized_text,
        "resolved_from": citation.resolved_from,
        "verification_status": citation.verification_status,
        "verification_detail": citation.verification_detail,
        "snippet": getattr(citation, "snippet", None),
    }


def run_to_context(run: AuditRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "created_at": run.created_at,
        "source_type": run.source_type,
        "source_name": run.source_name,
        "citation_count": run.citation_count,
        "verified_count": run.verified_count,
        "not_found_count": run.not_found_count,
        "ambiguous_count": run.ambiguous_count,
        "error_count": run.error_count,
        "unverified_no_token_count": run.unverified_no_token_count,
        "input_text_excerpt": run.input_text_excerpt,
        "warning_text": run.warning_text,
    }


def render_dashboard(
    request: Request,
    *,
    pasted_text: str = "",
    result_groups: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
    validation_message: str | None = None,
) -> HTMLResponse:
    groups = result_groups or []
    warnings = warnings or []
    total_citations = sum(group["citation_count"] for group in groups)

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "title": "Audit Dashboard",
            "pasted_text": pasted_text,
            "result_groups": groups,
            "warning_messages": warnings,
            "validation_message": validation_message,
            "total_citations": total_citations,
        },
    )


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    return render_dashboard(request)


@router.post("/audit", response_class=HTMLResponse)
async def run_audit(
    request: Request,
    pasted_text: str = PASTED_TEXT_FORM,
    uploaded_files: list[UploadFile] | None = UPLOADED_FILES_FORM,
) -> HTMLResponse:
    shared_warnings: list[str] = []
    result_groups: list[dict[str, Any]] = []

    sources, collection_warnings, validation_message = await collect_sources(
        pasted_text, uploaded_files
    )
    shared_warnings.extend(collection_warnings)

    if validation_message:
        return render_dashboard(
            request,
            pasted_text=pasted_text,
            warnings=shared_warnings,
            validation_message=validation_message,
        )

    for source in sources:
        started = perf_counter()

        citation_results, parsing_warnings = extract_citations(source.text)
        citation_results = resolve_id_citations(citation_results)
        citation_results = verify_citations(
            citation_results,
            courtlistener_token=settings.courtlistener_token,
            verification_base_url=settings.verification_base_url,
            verification_timeout_seconds=settings.verification_timeout_seconds,
        )

        group_warnings = [*source.warnings, *parsing_warnings]
        verification_summary = summarize_verification_statuses(citation_results)

        with db_session() as db:
            save_audit_run(
                db,
                source_type=source.source_type,
                source_name=source.source_name,
                input_text=source.text,
                warnings=group_warnings,
                citations=citation_results,
            )

        latency_ms = int((perf_counter() - started) * 1000)
        _record_event_safely(
            event_type="audit_completed",
            source_type=source.source_type,
            citation_count=len(citation_results),
            verified_count=verification_summary.get("VERIFIED", 0),
            not_found_count=verification_summary.get("NOT_FOUND", 0),
            ambiguous_count=verification_summary.get("AMBIGUOUS", 0)
            + verification_summary.get("DERIVED", 0),
            error_count=verification_summary.get("ERROR", 0),
            unverified_no_token_count=verification_summary.get("UNVERIFIED_NO_TOKEN", 0),
            had_warning=bool(group_warnings),
            latency_ms=latency_ms,
        )

        result_groups.append(
            {
                "source_type": source.source_type,
                "source_name": source.source_name,
                "citation_count": len(citation_results),
                "verification_summary": verification_summary,
                "citations": [citation_to_context(citation) for citation in citation_results],
                "warning_messages": group_warnings,
            }
        )

    return render_dashboard(
        request,
        pasted_text=pasted_text,
        result_groups=result_groups,
        warnings=shared_warnings,
    )


@router.get("/history", response_class=HTMLResponse)
def history(request: Request) -> HTMLResponse:
    with db_session() as db:
        runs = [run_to_context(run) for run in list_audit_runs(db)]

    _record_event_safely(event_type="history_viewed")

    return templates.TemplateResponse(
        request=request,
        name="history.html",
        context={
            "title": "History",
            "runs": runs,
        },
    )


@router.get("/history/{run_id}", response_class=HTMLResponse)
def history_detail(request: Request, run_id: int) -> HTMLResponse:
    with db_session() as db:
        run = get_audit_run(db, run_id)
        if run is None:
            _record_event_safely(event_type="missing_run_404")
            raise HTTPException(status_code=404, detail="Audit run not found")

        run_context = run_to_context(run)
        citations = [citation_to_context(citation) for citation in run.citations]

    _record_event_safely(event_type="history_detail_viewed")

    return templates.TemplateResponse(
        request=request,
        name="history_detail.html",
        context={
            "title": f"Audit Run #{run_id}",
            "run": run_context,
            "citations": citations,
        },
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={"title": "Settings"},
    )
