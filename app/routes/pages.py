import json
import logging
import re
from contextlib import contextmanager
from time import perf_counter
from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates

from aaa_db.models import AuditRun
from aaa_db.repository import (
    clear_cache_entry,
    clear_resolution_cache,
    get_audit_run,
    get_cache_stats,
    get_citation,
    get_user_run_number,
    list_audit_runs,
    lookup_resolution_cache,
    lookup_statute_cache,
    resolve_citation,
    save_audit_run,
    save_memo_for_run,
    save_statute_cache_entry,
)
from aaa_db.session import SessionLocal
from aaa_db.telemetry_repository import get_or_create_install_id, record_telemetry_event
from app.services.ai_risk_memo import (
    build_provider,
    generate_risk_memo,
    memo_from_json,
    memo_to_json,
    unavailable_memo,
)
from app.services.audit import (
    apply_citation_cap,
    collect_sources,
    extract_citations,
    resolve_id_citations,
    resolve_supra_citations,
)
from app.services.disambiguation import extract_case_name_from_text
from app.services.exporters import (
    export_csv_for_run,
    export_markdown_for_run,
    export_print_html_context,
)
from app.services.local_index import LocalIndexLookup
from app.services.local_index import get_stats as get_local_index_stats
from app.services.provenance import PROVENANCE_HELP, get_provenance, get_provenance_breakdown
from app.services.report_generator import generate_pdf_report
from app.services.search_links import build_search_links
from app.services.settings_service import (
    _SENSITIVE_KEYS,
    _is_masked,
    get_all_ui_settings,
    load_effective_settings,
    save_setting,
)
from app.services.verification import summarize_verification_statuses, verify_citations
from app.settings import TEMPLATES_DIR, settings

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

logger = logging.getLogger(__name__)


def _admin_unread_count(current_user: dict | None) -> int:
    """Jinja2 global: return unread ContactMessage count for user_id==1, else 0."""
    if not current_user or current_user.get("id") != 1:
        return 0
    try:
        from aaa_db.models import ContactMessage

        with db_session() as db:
            return db.query(ContactMessage).filter_by(is_read=False).count()
    except Exception:
        return 0


templates.env.globals["admin_unread_count"] = _admin_unread_count

_GOOD_STATUSES = frozenset({"VERIFIED", "DERIVED", "STATUTE_DETECTED", "STATUTE_VERIFIED"})


def _is_all_clean(citation_results: list) -> bool:
    return bool(citation_results) and all(
        (c.verification_status or "UNKNOWN") in _GOOD_STATUSES for c in citation_results
    )


def _is_courtlistener_unreachable(citation_results: list) -> bool:
    _skip = frozenset({"STATUTE_DETECTED", "STATUTE_VERIFIED", "DERIVED", "UNVERIFIED_NO_TOKEN"})
    attempted = [
        c
        for c in citation_results
        if (c.verification_status or "") not in _skip and c.resolution_method != "cache"
    ]
    return bool(attempted) and all(c.verification_status == "ERROR" for c in attempted)


PASTED_TEXT_FORM = Form(default="")
UPLOADED_FILES_FORM = File(default=None)


def _user_ctx(request: Request) -> dict | None:
    """Return session user dict or None if not logged in."""
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    return {
        "id": user_id,
        "email": request.session.get("user_email") or "",
        "name": request.session.get("user_name") or "",
    }


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


# ── Bluebook citation context extraction ─────────────────────────────────────

# Matches a parenthetical containing a 4-digit year, e.g. (1954), (4th Cir. 2001)
_YEAR_PAREN_RE = re.compile(r"\([^()]*\d{4}[^()]*\)")

# Matches a parallel reporter citation, e.g. "578 S.E.2d 781"
_PARALLEL_CITE_RE = re.compile(r"^\d+\s+[\w.]+\s+\d+$")

# Citation types that are back-references — context is the raw text itself
_BACK_REF_TYPES = {"IdCitation", "SupraCitation"}

# Words that signal a new sentence/citation context, not part of a party name
_CITATION_SIGNALS = frozenset(
    {
        "see",
        "cf.",
        "but",
        "also",
        "compare",
        "accord",
        "contra",
        "e.g.",
        "i.e.",
        "citing",
        "cited",
        "held",
        "said",
        "ruling",
        "finding",
        "noting",
    }
)

# Lowercase words that may appear inside a party name (e.g. "City of Charleston")
_NAME_PREPOSITIONS = frozenset({"of", "and", "de", "la", "le", "van", "von", "&"})


def _plaintiff_from_before_v(text: str) -> str | None:
    """Walk *text* backwards word-by-word to extract the plaintiff party name.

    Collects words that look like part of a proper name (title-case or a known
    name-preposition) and stops at sentence words, digits, or citation signals.
    Strips leading prepositions from the collected words before returning.
    """
    words = text.rstrip().split()
    if not words:
        return None

    result: list[str] = []
    for word in reversed(words):
        clean = word.rstrip(",.;:!?\"'")
        if not clean:
            continue
        if clean.lower() in _CITATION_SIGNALS:
            break
        if clean[0].isupper() or clean.lower() in _NAME_PREPOSITIONS:
            result.insert(0, clean)
            if len(result) >= 6:
                break
        else:
            # Lowercase word that is not a recognised preposition — sentence text
            break

    # Strip leading prepositions that don't belong at the start of a party name
    while result and result[0].lower() in _NAME_PREPOSITIONS:
        result.pop(0)

    return " ".join(result) if result else None


def _case_name_from_prefix(text: str) -> str | None:
    """Extract a case name from text that immediately precedes ', [reporter]'.

    Strips the trailing ', ' separator, then searches the text (split only on
    semicolons/newlines so that periods inside abbreviations like 'v.' or 'U.S.'
    are not treated as sentence boundaries) for one of three patterns:
      - "Party v. Party"
      - "In re Something"
      - "Ex parte Someone"

    Returns None when no reliable case name is found.
    """
    text = text.rstrip()
    if text.endswith(","):
        text = text[:-1].rstrip()
    if not text:
        return None

    # Split only on semicolons and newlines; period-based splitting would break
    # abbreviations like "v.", "U.S.", etc. that are common in citation text.
    segments = re.split(r";\s+|\n+", text)
    seg = segments[-1].strip() if segments else text.strip()

    # "In re ..." — match the last occurrence ending at the segment boundary
    in_re_matches = list(re.finditer(r"\b(In\s+re\s+[A-Z][^\n.!?;]{1,80})\s*$", seg, re.IGNORECASE))
    if in_re_matches:
        return in_re_matches[-1].group(1).strip()

    # "Ex parte ..." — same approach
    ex_parte_matches = list(
        re.finditer(r"\b(Ex\s+parte\s+[A-Z][^\n.!?;]{1,80})\s*$", seg, re.IGNORECASE)
    )
    if ex_parte_matches:
        return ex_parte_matches[-1].group(1).strip()

    # Standard "Party v. Party" — use the LAST "v." in the segment so that when
    # multiple case citations appear (e.g. "Harlow, 457 U.S. 800. Anderson v.
    # Creighton, 483 U.S. 635") we pick the one closest to the raw_text.
    v_matches = list(re.finditer(r"\bv\.\s+", seg))
    if not v_matches:
        return None

    last_v = v_matches[-1]
    defendant = seg[last_v.end() :].strip()
    plaintiff = _plaintiff_from_before_v(seg[: last_v.start()])

    if (
        plaintiff
        and defendant
        and not re.fullmatch(r"[\d\s]+", plaintiff)
        and not re.fullmatch(r"[\d\s]+", defendant)
        and len(plaintiff) <= 80
    ):
        return f"{plaintiff} v. {defendant}"

    return None


def _year_and_parallel_from_suffix(text: str) -> tuple[str | None, str | None]:
    """Extract the year parenthetical and any parallel citation from text after raw_text.

    Returns (year_paren, parallel), e.g. ("(4th Cir. 2001)", "578 S.E.2d 781").
    """
    m = _YEAR_PAREN_RE.search(text)
    if not m:
        return None, None

    year_paren = m.group(0)
    between = text[: m.start()].strip().lstrip(",").strip()
    parallel = between if _PARALLEL_CITE_RE.match(between) else None
    return year_paren, parallel


def _extract_bluebook_citation(snippet: str, raw_text: str) -> str | None:
    """Build a Bluebook-format citation string from a document snippet.

    Finds *raw_text* (e.g. "347 U.S. 483") inside *snippet*, then:
      - looks left for the case name ("Brown v. Board of Education")
      - looks right for a year parenthetical ("(1954)") and optional parallel
        reporter citation ("578 S.E.2d 781")

    Returns the assembled string, e.g.
      "Brown v. Board of Education, 347 U.S. 483 (1954)"
    or None when no reliable case name can be found.
    """
    if not snippet or not raw_text:
        return None

    idx = snippet.find(raw_text)
    if idx == -1:
        return None

    case_name = _case_name_from_prefix(snippet[:idx])
    if not case_name:
        return None

    year_paren, parallel = _year_and_parallel_from_suffix(snippet[idx + len(raw_text) :])

    parts = [f"{case_name}, {raw_text}"]
    if parallel:
        parts.append(f", {parallel}")
    if year_paren:
        parts.append(f" {year_paren}")
    return "".join(parts)


def citation_to_context(
    citation: Any,
    resolution_cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw_candidate_metadata = getattr(citation, "candidate_metadata", None)
    if isinstance(raw_candidate_metadata, str):
        try:
            raw_candidate_metadata = json.loads(raw_candidate_metadata)
        except (ValueError, TypeError):
            raw_candidate_metadata = None

    status = getattr(citation, "verification_status", None)
    resolution_method = getattr(citation, "resolution_method", None)

    search_links: dict[str, str] | None = None
    if status == "NOT_FOUND":
        raw_text = getattr(citation, "raw_text", "") or ""
        snippet = getattr(citation, "snippet", None)
        case_name: str | None = None
        for src in (snippet, raw_text):
            if src:
                case_name = extract_case_name_from_text(src)
                if case_name:
                    break
        search_links = build_search_links(raw_text, case_name)

    # Determine original resolution method and trust tier for cached citations so the
    # provenance label shows e.g. "Direct Match (cached)" rather than just "cached".
    original_method: str | None = None
    cache_trust_tier: str | None = None
    if resolution_method == "cache" and resolution_cache is not None:
        cache_key = getattr(citation, "normalized_text", None) or getattr(citation, "raw_text", "")
        cached_entry = resolution_cache.get(cache_key)
        if cached_entry:
            original_method = cached_entry.get("resolution_method")
            cache_trust_tier = cached_entry.get("trust_tier")

    provenance = get_provenance(
        status, resolution_method, original_method, trust_tier=cache_trust_tier
    )

    # Derive citation context: build a Bluebook-format string for case law, or
    # use raw_text as-is for statutes and back-references.
    citation_context: str | None = None
    raw_text_val = getattr(citation, "raw_text", "") or ""
    snippet_val = getattr(citation, "snippet", None)
    citation_type_val = getattr(citation, "citation_type", "") or ""
    if citation_type_val in _BACK_REF_TYPES or citation_type_val == "FullLawCitation":
        # Statutes and back-references: raw text is the context
        citation_context = raw_text_val or None
    elif snippet_val:
        # Case law: extract Bluebook format from snippet; fall back to raw_text
        citation_context = (
            _extract_bluebook_citation(snippet_val, raw_text_val) or raw_text_val or None
        )
    else:
        citation_context = raw_text_val or None

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
        "resolution_method": resolution_method,
        "search_links": search_links,
        "provenance": provenance,
        "cache_trust_tier": cache_trust_tier,
        "citation_context": citation_context,
    }


def run_to_context(run: AuditRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "created_at": run.created_at,
        "source_type": run.source_type,
        "source_name": run.source_name,
        "audit_mode": getattr(run, "audit_mode", "self_review") or "self_review",
        "citation_count": run.citation_count,
        "verified_count": run.verified_count,
        "not_found_count": run.not_found_count,
        "ambiguous_count": run.ambiguous_count,
        "derived_count": run.derived_count,
        "statute_count": run.statute_count,
        "statute_verified_count": run.statute_verified_count or 0,
        "error_count": run.error_count,
        "unverified_no_token_count": run.unverified_no_token_count,
        "input_text_excerpt": run.input_text_excerpt,
        "warning_text": run.warning_text,
    }


_RISKY_STATUSES = frozenset({"NOT_FOUND", "AMBIGUOUS", "ERROR"})


def build_ai_memo_input(
    *,
    source_type: str,
    source_name: str | None,
    verification_summary: dict[str, int],
    citations: list[dict[str, str | None]],
    warnings: list[str],
    include_content: bool,
    audit_mode: str = "self_review",
) -> dict[str, Any]:
    # Build a parent-status lookup keyed by raw_text for DERIVED parent resolution.
    parent_status: dict[str, str] = {
        c["raw_text"]: c.get("verification_status") or ""
        for c in citations
        if c.get("raw_text") and c.get("verification_status") != "DERIVED"
    }

    derived_verified_parent = 0
    derived_risky_parent = 0
    for c in citations:
        if c.get("verification_status") != "DERIVED":
            continue
        parent_raw = c.get("resolved_from")
        pstatus = parent_status.get(parent_raw or "", "") if parent_raw else ""
        if pstatus == "VERIFIED":
            derived_verified_parent += 1
        elif pstatus in _RISKY_STATUSES or not pstatus:
            derived_risky_parent += 1

    payload: dict[str, Any] = {
        "source_type": source_type,
        "source_name": source_name,
        "audit_mode": audit_mode,
        "verification_summary": verification_summary,
        "citation_count": len(citations),
        "warnings_present": bool(warnings),
        "derived_verified_parent_count": derived_verified_parent,
        "derived_risky_parent_count": derived_risky_parent,
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
    audit_mode: str = "self_review",
    effective_settings=None,
):
    eff = effective_settings or settings
    run_data = build_ai_memo_input(
        source_type=source_type,
        source_name=source_name,
        verification_summary=verification_summary,
        citations=citations,
        warnings=warnings,
        include_content=eff.ai_memo_include_content,
        audit_mode=audit_mode,
    )

    provider = build_provider(eff)
    try:
        return generate_risk_memo(
            run_data,
            enabled=eff.ai_provider != "none",
            api_key=eff.openai_api_key,
            model=eff.ai_memo_model,
            timeout_seconds=eff.ai_request_timeout_seconds,
            provider=provider,
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
    audit_mode: str = "self_review",
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
            "provenance_help": PROVENANCE_HELP,
            "current_user": _user_ctx(request),
            "audit_mode": audit_mode,
        },
    )


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    if request.session.get("user_id") is None:
        return templates.TemplateResponse(
            request=request,
            name="landing.html",
            context={"title": "FinalVerify — Citation Verification for Legal Professionals"},
        )
    return render_dashboard(request)


@router.post("/audit", response_class=HTMLResponse)
async def run_audit(
    request: Request,
    pasted_text: str = PASTED_TEXT_FORM,
    uploaded_files: list[UploadFile] | None = UPLOADED_FILES_FORM,
    audit_mode: str = Form(default="self_review"),
) -> HTMLResponse:
    current_user = _user_ctx(request)
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

    with db_session() as db:
        eff = load_effective_settings(db)

    sources, collection_warnings, validation_message = await collect_sources(
        pasted_text,
        uploaded_files,
        max_files=eff.max_files_per_batch,
        max_file_size_mb=eff.max_file_size_mb,
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

    # Sanitise audit_mode — only accept known values
    safe_mode = audit_mode if audit_mode in ("self_review", "opposing_review") else "self_review"

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
            citation_results, eff.max_citations_per_run
        )
        citation_results = resolve_id_citations(citation_results)
        citation_results = resolve_supra_citations(citation_results)

        group_warnings = [*source.warnings, *parsing_warnings]
        if cap_warning:
            logger.warning("Citation cap triggered for %r: %s", source.source_name, cap_warning)
            group_warnings.insert(0, cap_warning)

        if not citation_results:
            latency_ms = int((perf_counter() - started) * 1000)
            logger.info(
                "No citations found: source=%r, latency_ms=%d",
                source.source_name,
                latency_ms,
            )
            result_groups.append(
                {
                    "run_id": None,
                    "source_type": source.source_type,
                    "source_name": source.source_name,
                    "citation_count": 0,
                    "verification_summary": {},
                    "citations": [],
                    "warning_messages": group_warnings,
                    "citation_cap_warning": cap_warning,
                    "no_citations": True,
                    "all_clean": False,
                    "courtlistener_unreachable": False,
                    "ai_memo": None,
                }
            )
            continue

        with db_session() as db:
            cache = lookup_resolution_cache(
                db, current_user_id=current_user["id"] if current_user else None
            )
            statute_cache = lookup_statute_cache(db)
            local_index = LocalIndexLookup(db) if eff.local_index_enabled else None
        citation_results = verify_citations(
            citation_results,
            courtlistener_token=eff.courtlistener_token,
            verification_base_url=eff.verification_base_url,
            courtlistener_timeout_seconds=eff.courtlistener_timeout_seconds,
            batch_verification=eff.batch_verification,
            resolution_cache=cache,
            search_fallback_enabled=eff.search_fallback_enabled,
            virginia_statute_verification=eff.virginia_statute_verification,
            statute_cache=statute_cache,
            virginia_statute_timeout_seconds=eff.virginia_statute_timeout_seconds,
            federal_statute_verification=eff.federal_statute_verification,
            govinfo_api_key=eff.govinfo_api_key,
            federal_statute_timeout_seconds=eff.federal_statute_timeout_seconds,
            cap_fallback_enabled=eff.cap_fallback_enabled,
            cap_api_key=eff.cap_api_key,
            cap_timeout_seconds=eff.cap_timeout_seconds,
            local_index=local_index,
            local_index_enabled=eff.local_index_enabled,
        )
        # Persist any new statute verification results to the cache
        with db_session() as db:
            for section_num, entry in statute_cache.items():
                save_statute_cache_entry(
                    db,
                    section_number=section_num,
                    status=entry["status"],
                    section_title=entry.get("section_title"),
                )

        verification_summary = summarize_verification_statuses(citation_results)

        with db_session() as db:
            run = save_audit_run(
                db,
                source_type=source.source_type,
                source_name=source.source_name,
                input_text=source.text,
                warnings=group_warnings,
                citations=citation_results,
                user_id=current_user["id"] if current_user else None,
                audit_mode=safe_mode,
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
            statute_verified_count=verification_summary.get("STATUTE_VERIFIED", 0),
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
                "audit_mode": safe_mode,
                "citation_count": len(citation_results),
                "verification_summary": verification_summary,
                "citations": [
                    citation_to_context(c, resolution_cache=cache) for c in citation_results
                ],
                "provenance_breakdown": get_provenance_breakdown(
                    citation_results, resolution_cache=cache
                ),
                "warning_messages": group_warnings,
                "citation_cap_warning": cap_warning,
                "no_citations": False,
                "all_clean": _is_all_clean(citation_results),
                "courtlistener_unreachable": _is_courtlistener_unreachable(citation_results),
            }
        )

        ai_memo = generate_ai_memo_for_group(
            source_type=result_groups[-1]["source_type"],
            source_name=result_groups[-1]["source_name"],
            verification_summary=result_groups[-1]["verification_summary"],
            citations=result_groups[-1]["citations"],
            warnings=group_warnings,
            audit_mode=safe_mode,
            effective_settings=eff,
        )
        result_groups[-1]["ai_memo"] = ai_memo

        # Persist the memo so history views show the same text without regenerating.
        with db_session() as db:
            save_memo_for_run(db, run.id, memo_to_json(ai_memo))

    return render_dashboard(
        request,
        pasted_text=pasted_text,
        result_groups=result_groups,
        warnings=shared_warnings,
        audit_mode=safe_mode,
    )


@router.get("/history", response_class=HTMLResponse)
def history(request: Request) -> HTMLResponse:
    current_user = _user_ctx(request)
    user_id = current_user["id"] if current_user else None
    with db_session() as db:
        run_objects = list_audit_runs(db, user_id=user_id)
        total = len(run_objects)
        runs = []
        for i, run_obj in enumerate(run_objects):
            ctx = run_to_context(run_obj)
            ctx["user_run_number"] = total - i
            runs.append(ctx)

    _record_event_safely(event_type="history_viewed")

    return templates.TemplateResponse(
        request=request,
        name="history.html",
        context={
            "title": "History",
            "runs": runs,
            "current_user": current_user,
        },
    )


@router.get("/history/{run_id}", response_class=HTMLResponse)
def history_detail(request: Request, run_id: int) -> HTMLResponse:
    current_user = _user_ctx(request)
    user_id = current_user["id"] if current_user else None
    with db_session() as db:
        run = get_audit_run(db, run_id, user_id=user_id)
        if run is None:
            _record_event_safely(event_type="missing_run_404")
            raise HTTPException(status_code=404, detail="Audit run not found")

        run_context = run_to_context(run)
        user_run_number = get_user_run_number(db, run_id, user_id)
        cache = lookup_resolution_cache(db, current_user_id=user_id)
        citations = [
            citation_to_context(citation, resolution_cache=cache) for citation in run.citations
        ]
        provenance_breakdown = get_provenance_breakdown(run.citations, resolution_cache=cache)

        # Load the persisted memo — never regenerate on history views so results are stable.
        if run.memo_json:
            try:
                ai_memo = memo_from_json(run.memo_json)
            except Exception:
                logger.warning("Failed to deserialize memo_json for run id=%d", run_id)
                ai_memo = unavailable_memo(
                    "Stored memo could not be read. Click 'Regenerate memo' to create a new one."
                )
        else:
            ai_memo = unavailable_memo(
                "No memo was stored for this audit run. "
                "Click 'Regenerate memo' below to generate one."
            )

    _record_event_safely(event_type="history_detail_viewed")

    return templates.TemplateResponse(
        request=request,
        name="history_detail.html",
        context={
            "title": f"Audit Run #{user_run_number}",
            "run": run_context,
            "audit_mode": run_context.get("audit_mode", "self_review"),
            "user_run_number": user_run_number,
            "citations": citations,
            "provenance_breakdown": provenance_breakdown,
            "provenance_help": PROVENANCE_HELP,
            "ai_memo": ai_memo,
            "current_user": current_user,
        },
    )


@router.post("/history/{run_id}/regenerate-memo", response_class=HTMLResponse)
def regenerate_memo(request: Request, run_id: int) -> RedirectResponse:
    """Explicitly regenerate the AI memo for a saved run and re-persist it."""
    current_user = _user_ctx(request)
    with db_session() as db:
        run = get_audit_run(db, run_id, user_id=current_user["id"] if current_user else None)
        if run is None:
            raise HTTPException(status_code=404, detail="Audit run not found")

        run_context = run_to_context(run)
        cache = lookup_resolution_cache(
            db, current_user_id=current_user["id"] if current_user else None
        )
        citations_ctx = [citation_to_context(c, resolution_cache=cache) for c in run.citations]
        verification_summary = summarize_verification_statuses(run.citations)

    with db_session() as db:
        eff = load_effective_settings(db)

    ai_memo = generate_ai_memo_for_group(
        source_type=run_context["source_type"],
        source_name=run_context["source_name"],
        verification_summary=verification_summary,
        citations=citations_ctx,
        warnings=[run_context["warning_text"]] if run_context["warning_text"] else [],
        audit_mode=run_context.get("audit_mode", "self_review"),
        effective_settings=eff,
    )

    with db_session() as db:
        save_memo_for_run(db, run_id, memo_to_json(ai_memo))

    logger.info("AI memo regenerated for run id=%d", run_id)
    return RedirectResponse(url=f"/history/{run_id}", status_code=303)


@router.get("/history/{run_id}/export")
def export_run(request: Request, run_id: int, format: str = Query(default="markdown")) -> Response:
    current_user = _user_ctx(request)
    with db_session() as db:
        run = get_audit_run(db, run_id, user_id=current_user["id"] if current_user else None)
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


@router.get("/history/{run_id}/report")
def download_report(request: Request, run_id: int) -> Response:
    """Generate and return a professional Citation Verification Report as PDF."""
    current_user = _user_ctx(request)
    user_id = current_user["id"] if current_user else None
    with db_session() as db:
        run = get_audit_run(db, run_id, user_id=user_id)
        if run is None:
            _record_event_safely(event_type="missing_run_404")
            raise HTTPException(status_code=404, detail="Audit run not found")
        user_run_number = get_user_run_number(db, run_id, user_id)

    _record_event_safely(event_type="report_downloaded", source_type=run.source_type)

    pdf_bytes = generate_pdf_report(
        run,
        user_run_number=user_run_number,
        user_email=current_user["email"] if current_user else "",
    )
    filename = f"citation-verification-report-{run_id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/history/{run_id}/citations/{citation_id}/resolve")
def resolve_citation_route(
    request: Request,
    run_id: int,
    citation_id: int,
    cluster_id: int = Form(...),
) -> RedirectResponse:
    current_user = _user_ctx(request)
    with db_session() as db:
        run_check = get_audit_run(db, run_id, user_id=current_user["id"] if current_user else None)
        if run_check is None:
            raise HTTPException(status_code=404, detail="Audit run not found")
        citation = get_citation(db, citation_id)
        if citation is None or citation.audit_run_id != run_id:
            raise HTTPException(status_code=404, detail="Citation not found")

        # Validate that cluster_id is in the stored candidate list.
        candidate_ids: list[int] | None = None
        if citation.candidate_cluster_ids:
            try:
                candidate_ids = json.loads(citation.candidate_cluster_ids)
            except (ValueError, TypeError):
                candidate_ids = None

        if candidate_ids is not None and cluster_id not in candidate_ids:
            raise HTTPException(
                status_code=400,
                detail="Selected case is not in the candidate list for this citation",
            )
        if candidate_ids is None:
            logger.warning(
                "Citation id=%d has no stored candidate list; allowing resolution without"
                " validation",
                citation_id,
            )

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
            user_id=current_user["id"] if current_user else None,
        )

    logger.info(
        "User resolved citation id=%d to cluster_id=%d in run %d",
        citation_id,
        cluster_id,
        run_id,
    )
    return RedirectResponse(url=f"/history/{run_id}", status_code=303)


@router.post("/cache/report-incorrect")
async def report_cache_incorrect(
    request: Request,
    normalized_cite: str = Form(...),
    redirect_to: str = Form(default="/history"),
) -> RedirectResponse:
    """Clear a user_submitted cache entry reported as incorrect."""
    with db_session() as db:
        cleared = clear_cache_entry(db, normalized_cite)
    if cleared:
        logger.info("Cache entry cleared via report-incorrect: %r", normalized_cite)
    else:
        logger.debug(
            "report-incorrect: entry %r not cleared (not found or protected)", normalized_cite
        )
    return RedirectResponse(url=redirect_to, status_code=303)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: bool = False) -> HTMLResponse:
    with db_session() as db:
        ui = get_all_ui_settings(db)
        cache_stats = get_cache_stats(db)
        local_index_stats = get_local_index_stats(db)
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "title": "Settings",
            "ui": ui,
            "saved": saved,
            "cache_stats": cache_stats,
            "local_index_stats": local_index_stats,
            "current_user": _user_ctx(request),
        },
    )


# Keys accepted from the settings form (whitelist)
_FORM_KEYS = [
    "courtlistener_token",
    "verification_base_url",
    "courtlistener_timeout_seconds",
    "search_fallback_enabled",
    "ai_provider",
    "openai_api_key",
    "ai_memo_model",
    "ai_request_timeout_seconds",
    "ai_memo_include_content",
    "ollama_base_url",
    "ollama_model",
    "virginia_statute_verification",
    "virginia_statute_timeout_seconds",
    "govinfo_api_key",
    "federal_statute_verification",
    "federal_statute_timeout_seconds",
    "cap_api_key",
    "cap_fallback_enabled",
    "cap_timeout_seconds",
    "local_index_enabled",
    "max_file_size_mb",
    "max_files_per_batch",
    "max_citations_per_run",
    "log_level",
]

# Form keys that are boolean checkboxes (absent = false when unchecked)
_CHECKBOX_KEYS = {
    "ai_memo_include_content",
    "search_fallback_enabled",
    "virginia_statute_verification",
    "federal_statute_verification",
    "cap_fallback_enabled",
    "local_index_enabled",
}


@router.post("/settings", response_class=HTMLResponse)
async def save_settings(request: Request) -> RedirectResponse:
    form = await request.form()

    with db_session() as db:
        for key in _FORM_KEYS:
            # Checkboxes are absent from form data when unchecked
            if key in _CHECKBOX_KEYS:
                value = "true" if form.get(key) else "false"
                save_setting(db, key, value)
                continue

            raw = str(form.get(key, "")).strip()

            # Sensitive fields: if still masked, skip (don't overwrite)
            if key in _SENSITIVE_KEYS and _is_masked(raw):
                continue

            # Store non-empty values; skip empty sensitive fields
            if not raw and key in _SENSITIVE_KEYS:
                continue

            save_setting(db, key, raw or None)

    logger.info("Settings saved via UI")
    return RedirectResponse(url="/settings?saved=1", status_code=303)


@router.post("/settings/clear-cache", response_class=HTMLResponse)
def clear_cache(request: Request) -> HTMLResponse:
    with db_session() as db:
        count = clear_resolution_cache(db)
        ui = get_all_ui_settings(db)
        cache_stats = get_cache_stats(db)
        local_index_stats = get_local_index_stats(db)
    logger.info("Resolution cache cleared via settings: %d entries removed", count)
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "title": "Settings",
            "ui": ui,
            "cache_cleared": True,
            "cache_cleared_count": count,
            "cache_stats": cache_stats,
            "local_index_stats": local_index_stats,
            "current_user": _user_ctx(request),
        },
    )


@router.post("/waitlist", response_class=HTMLResponse)
async def join_waitlist(
    request: Request,
    background_tasks: BackgroundTasks,
    email: str = Form(...),
) -> JSONResponse:
    from sqlalchemy.exc import IntegrityError

    from aaa_db.models import WaitlistEntry
    from app.services.notifications import send_waitlist_notification

    email = email.strip().lower()
    if not email or "@" not in email:
        return JSONResponse({"ok": False, "error": "Invalid email address."}, status_code=400)

    try:
        with db_session() as db:
            db.add(WaitlistEntry(email=email))
            db.commit()
        background_tasks.add_task(send_waitlist_notification, email)
    except IntegrityError:
        logger.debug("Waitlist: duplicate email ignored: %s", email)

    return JSONResponse({"ok": True})


@router.post("/contact")
async def submit_contact(
    request: Request,
    background_tasks: BackgroundTasks,
    name: str = Form(...),
    organization: str = Form(default=""),
    email: str = Form(...),
    subject: str = Form(...),
    message: str = Form(...),
) -> JSONResponse:
    from aaa_db.models import ContactMessage
    from app.services.notifications import send_contact_notification

    name = name.strip()
    email = email.strip()
    subject = subject.strip()
    message = message.strip()
    organization = organization.strip()

    if not all([name, email, subject, message]) or "@" not in email:
        return JSONResponse({"ok": False, "error": "All fields are required."}, status_code=400)

    with db_session() as db:
        db.add(
            ContactMessage(
                name=name,
                organization=organization or None,
                email=email,
                subject=subject,
                message=message,
            )
        )
        db.commit()

    background_tasks.add_task(
        send_contact_notification, name, email, subject, message, organization
    )
    return JSONResponse({"ok": True})


@router.get("/admin/messages", response_class=HTMLResponse)
def admin_messages(request: Request) -> HTMLResponse:
    current_user = _user_ctx(request)
    if not current_user or current_user["id"] != 1:
        raise HTTPException(status_code=404)

    from aaa_db.models import ContactMessage

    with db_session() as db:
        rows = db.query(ContactMessage).order_by(ContactMessage.created_at.desc()).all()
        msgs = [
            {
                "id": m.id,
                "name": m.name,
                "organization": m.organization,
                "email": m.email,
                "subject": m.subject,
                "message_preview": m.message[:120],
                "is_read": m.is_read,
                "created_at": m.created_at,
            }
            for m in rows
        ]

    return templates.TemplateResponse(
        request=request,
        name="admin_messages.html",
        context={"title": "Messages", "messages": msgs, "current_user": current_user},
    )


@router.get("/admin/messages/{msg_id}", response_class=HTMLResponse)
def admin_message_detail(request: Request, msg_id: int) -> HTMLResponse:
    current_user = _user_ctx(request)
    if not current_user or current_user["id"] != 1:
        raise HTTPException(status_code=404)

    from aaa_db.models import ContactMessage

    with db_session() as db:
        msg = db.get(ContactMessage, msg_id)
        if msg is None:
            raise HTTPException(status_code=404)
        msg.is_read = True
        db.commit()
        msg_ctx = {
            "id": msg.id,
            "name": msg.name,
            "organization": msg.organization,
            "email": msg.email,
            "subject": msg.subject,
            "message": msg.message,
            "is_read": True,
            "created_at": msg.created_at,
        }

    return templates.TemplateResponse(
        request=request,
        name="admin_message_detail.html",
        context={
            "title": f"Message from {msg_ctx['name']}",
            "msg": msg_ctx,
            "current_user": current_user,
        },
    )


@router.post("/admin/test-email")
def admin_test_email(
    request: Request,
    recipient: str = Form(...),
) -> JSONResponse:
    """Send a one-shot test email through the Resend pipeline.

    Restricted to user_id==1 (the admin convention used by the rest of
    the /admin/* surface). Returns the Resend message_id on success so
    delivery can be verified end-to-end without exercising a form.
    """
    current_user = _user_ctx(request)
    if not current_user or current_user["id"] != 1:
        raise HTTPException(status_code=404)

    from app.services.notifications import send_test_email

    recipient = recipient.strip().lower()
    if not recipient or "@" not in recipient:
        return JSONResponse(
            {"ok": False, "error": "Invalid email address."}, status_code=400
        )

    result = send_test_email(recipient)
    if result.success:
        return JSONResponse({"ok": True, "message_id": result.message_id})
    return JSONResponse(
        {"ok": False, "error": result.error or "Unknown send failure"},
        status_code=502,
    )


@router.get("/about", response_class=HTMLResponse)
def about_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="about.html",
        context={"title": "About FinalVerify"},
    )


@router.get("/contact", response_class=HTMLResponse)
def contact_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="contact.html",
        context={"title": "Contact"},
    )


@router.get("/privacy", response_class=HTMLResponse)
def privacy_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="privacy.html",
        context={"title": "Privacy Policy"},
    )
