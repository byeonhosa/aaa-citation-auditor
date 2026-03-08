import json
import logging
from contextlib import contextmanager
from time import perf_counter
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from aaa_db.models import AuditRun
from aaa_db.repository import (
    clear_resolution_cache,
    get_audit_run,
    get_citation,
    list_audit_runs,
    lookup_resolution_cache,
    resolve_citation,
    save_audit_run,
)
from aaa_db.session import SessionLocal
from aaa_db.telemetry_repository import get_or_create_install_id, record_telemetry_event
from app.services.ai_risk_memo import generate_risk_memo, unavailable_memo
from app.services.audit import (
    apply_citation_cap,
    collect_sources,
    extract_citations,
    resolve_id_citations,
)
from app.services.exporters import (
    export_csv_for_run,
    export_markdown_for_run,
    export_print_html_context,
)
from app.services.verification import summarize_verification_statuses, verify_citations
from app.settings import TEMPLATES_DIR, settings

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

logger = logging.getLogger(__name__)

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
        logger.exception("Failed to record telemetry event.")


def citation_to_context(citation: Any) -> dict[str, Any]:
    raw_candidate_metadata = getattr(citation, "candidate_metadata", None)
    if isinstance(raw_candidate_metadata, str):
        try:
            raw_candidate_metadata = json.loads(raw_candidate_metadata)
        except (ValueError, TypeError):
            raw_candidate_metadata = None

    return {
        "id": getattr(citation, "id", None),
        "raw_text": citation.raw_text,
        "citation_type": citation.citation_type,
        "normalized_text": citation.normalized_text,
        "resolved_from": citation.resolved_from,
        "verification_status": citation.verification_status,
        "verification_detail": citation.verification_detail,
        "snippet": getattr(citation, "snippet", None),
        "candidate_metadata": raw_candidate_metadata,
        "selected_cluster_id": getattr(citation, "selected_cluster_id", None),
        "resolution_method": getattr(citation, "resolution_method", None),
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
        "derived_count": run.derived_count,
        "statute_count": run.statute_count,
        "error_count": run.error_count,
        "unverified_no_token_count": run.unverified_no_token_count,
        "input_text_excerpt": run.input_text_excerpt,
        "warning_text": run.warning_text,
    }


def build_ai_memo_input(
    *,
    source_type: str,
    source_name: str | None,
    verification_summary: dict[str, int],
    citations: list[dict[str, str | None]],
    warnings: list[str],
    include_content: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source_type": source_type,
        "source_name": source_name,
        "verification_summary": verification_summary,
        "citation_count": len(citations),
        "warnings_present": bool(warnings),
    }

    if include_content:
        payload["citations"] = [
            {
                "raw_text": citation.get("raw_text"),
                "citation_type": citation.get("citation_type"),
                "resolved_from": citation.get("resolved_from"),
                "verification_status": citation.get("verification_status"),
                "verification_detail": citation.get("verification_detail"),
                "snippet": citation.get("snippet"),
            }
            for citation in citations
        ]
        payload["warnings"] = warnings

    return payload


def generate_ai_memo_for_group(
    *,
    source_type: str,
    source_name: str | None,
    verification_summary: dict[str, int],
    citations: list[dict[str, str | None]],
    warnings: list[str],
):
    run_data = build_ai_memo_input(
        source_type=source_type,
        source_name=source_name,
        verification_summary=verification_summary,
        citations=citations,
        warnings=warnings,
        include_content=settings.ai_memo_include_content,
    )

    try:
        return generate_risk_memo(
            run_data,
            enabled=settings.ai_memo_enabled,
            api_key=settings.openai_api_key,
            model=settings.ai_memo_model,
            timeout_seconds=settings.ai_request_timeout_seconds,
        )
    except Exception:
        return unavailable_memo("AI memo generation failed.")


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

    valid_file_count = sum(1 for f in (uploaded_files or []) if f and f.filename)
    text_len = len((pasted_text or "").strip())
    source_desc = "text" if text_len else f"{valid_file_count} file(s)"
    logger.info(
        "Audit request received: source=%s, text_len=%d, files=%d",
        source_desc,
        text_len,
        valid_file_count,
    )

    sources, collection_warnings, validation_message = await collect_sources(
        pasted_text,
        uploaded_files,
        max_files=settings.max_files_per_batch,
        max_file_size_mb=settings.max_file_size_mb,
    )
    shared_warnings.extend(collection_warnings)

    if validation_message:
        logger.warning("Audit request rejected by guardrail: %s", validation_message)
        return render_dashboard(
            request,
            pasted_text=pasted_text,
            warnings=shared_warnings,
            validation_message=validation_message,
        )

    for source in sources:
        started = perf_counter()
        logger.info(
            "Processing source: type=%s, name=%r, text_len=%d",
            source.source_type,
            source.source_name,
            len(source.text),
        )

        citation_results, parsing_warnings = extract_citations(source.text)
        citation_results, cap_warning = apply_citation_cap(
            citation_results, settings.max_citations_per_run
        )
        citation_results = resolve_id_citations(citation_results)
        with db_session() as db:
            cache = lookup_resolution_cache(db)
        citation_results = verify_citations(
            citation_results,
            courtlistener_token=settings.courtlistener_token,
            verification_base_url=settings.verification_base_url,
            courtlistener_timeout_seconds=settings.courtlistener_timeout_seconds,
            batch_verification=settings.batch_verification,
            resolution_cache=cache,
        )

        group_warnings = [*source.warnings, *parsing_warnings]
        if cap_warning:
            logger.warning("Citation cap triggered for %r: %s", source.source_name, cap_warning)
            group_warnings.insert(0, cap_warning)
        verification_summary = summarize_verification_statuses(citation_results)

        with db_session() as db:
            run = save_audit_run(
                db,
                source_type=source.source_type,
                source_name=source.source_name,
                input_text=source.text,
                warnings=group_warnings,
                citations=citation_results,
            )

        latency_ms = int((perf_counter() - started) * 1000)
        logger.info(
            "Audit complete: source=%r, citations=%d, latency_ms=%d",
            source.source_name,
            len(citation_results),
            latency_ms,
        )
        _record_event_safely(
            event_type="audit_completed",
            source_type=source.source_type,
            citation_count=len(citation_results),
            verified_count=verification_summary.get("VERIFIED", 0),
            not_found_count=verification_summary.get("NOT_FOUND", 0),
            ambiguous_count=verification_summary.get("AMBIGUOUS", 0),
            derived_count=verification_summary.get("DERIVED", 0),
            statute_count=verification_summary.get("STATUTE_DETECTED", 0),
            error_count=verification_summary.get("ERROR", 0),
            unverified_no_token_count=verification_summary.get("UNVERIFIED_NO_TOKEN", 0),
            had_warning=bool(group_warnings),
            latency_ms=latency_ms,
        )

        result_groups.append(
            {
                "run_id": run.id,
                "source_type": source.source_type,
                "source_name": source.source_name,
                "citation_count": len(citation_results),
                "verification_summary": verification_summary,
                "citations": [citation_to_context(citation) for citation in citation_results],
                "warning_messages": group_warnings,
                "citation_cap_warning": cap_warning,
            }
        )

        result_groups[-1]["ai_memo"] = generate_ai_memo_for_group(
            source_type=result_groups[-1]["source_type"],
            source_name=result_groups[-1]["source_name"],
            verification_summary=result_groups[-1]["verification_summary"],
            citations=result_groups[-1]["citations"],
            warnings=group_warnings,
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
        verification_summary = summarize_verification_statuses(run.citations)

    ai_memo = generate_ai_memo_for_group(
        source_type=run_context["source_type"],
        source_name=run_context["source_name"],
        verification_summary=verification_summary,
        citations=citations,
        warnings=[run_context["warning_text"]] if run_context["warning_text"] else [],
    )

    _record_event_safely(event_type="history_detail_viewed")

    return templates.TemplateResponse(
        request=request,
        name="history_detail.html",
        context={
            "title": f"Audit Run #{run_id}",
            "run": run_context,
            "citations": citations,
            "ai_memo": ai_memo,
        },
    )


@router.get("/history/{run_id}/export")
def export_run(request: Request, run_id: int, format: str = Query(default="markdown")) -> Response:
    with db_session() as db:
        run = get_audit_run(db, run_id)
        if run is None:
            _record_event_safely(event_type="missing_run_404")
            raise HTTPException(status_code=404, detail="Audit run not found")

    _record_event_safely(event_type="export_generated", source_type=run.source_type)

    file_stub = f"audit-run-{run.id}"
    if format == "csv":
        content = export_csv_for_run(run)
        return Response(
            content=content,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{file_stub}.csv"'},
        )

    if format in {"markdown", "md"}:
        content = export_markdown_for_run(run)
        return PlainTextResponse(
            content=content,
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="{file_stub}.md"'},
        )

    if format in {"html", "print"}:
        context = export_print_html_context(run)
        return templates.TemplateResponse(
            request=request,
            name="print_export.html",
            context=context,
        )

    raise HTTPException(status_code=400, detail="Unsupported export format")


@router.post("/history/{run_id}/citations/{citation_id}/resolve")
def resolve_citation_route(
    request: Request,
    run_id: int,
    citation_id: int,
    cluster_id: int = Form(...),
) -> RedirectResponse:
    with db_session() as db:
        citation = get_citation(db, citation_id)
        if citation is None or citation.audit_run_id != run_id:
            raise HTTPException(status_code=404, detail="Citation not found")

        raw_candidate_metadata = citation.candidate_metadata
        candidate_metadata: list[dict] | None = None
        if isinstance(raw_candidate_metadata, str):
            try:
                candidate_metadata = json.loads(raw_candidate_metadata)
            except (ValueError, TypeError):
                candidate_metadata = None

        resolve_citation(
            db,
            citation,
            selected_cluster_id=cluster_id,
            resolution_method="user",
            candidate_metadata=candidate_metadata,
        )

    logger.info(
        "User resolved citation id=%d to cluster_id=%d in run %d",
        citation_id,
        cluster_id,
        run_id,
    )
    return RedirectResponse(url=f"/history/{run_id}", status_code=303)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={"title": "Settings"},
    )


@router.post("/settings/clear-cache", response_class=HTMLResponse)
def clear_cache(request: Request) -> HTMLResponse:
    with db_session() as db:
        count = clear_resolution_cache(db)
    logger.info("Resolution cache cleared via settings: %d entries removed", count)
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "title": "Settings",
            "cache_cleared": True,
            "cache_cleared_count": count,
        },
    )
