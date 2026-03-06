from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from aaa_db.models import TelemetryEvent
from app.settings import PROJECT_ROOT

INSTALL_ID_PATH = PROJECT_ROOT / ".aaa" / "install_id"


def get_or_create_install_id(path: Path = INSTALL_ID_PATH) -> str:
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value

    path.parent.mkdir(parents=True, exist_ok=True)
    install_id = str(uuid4())
    path.write_text(install_id, encoding="utf-8")
    return install_id


def record_telemetry_event(
    db: Session,
    *,
    event_type: str,
    install_id: str,
    app_version: str | None,
    source_type: str | None = None,
    citation_count: int | None = None,
    verified_count: int | None = None,
    not_found_count: int | None = None,
    ambiguous_count: int | None = None,
    derived_count: int | None = None,
    error_count: int | None = None,
    unverified_no_token_count: int | None = None,
    had_warning: bool = False,
    latency_ms: int | None = None,
) -> TelemetryEvent:
    event = TelemetryEvent(
        event_type=event_type,
        install_id=install_id,
        app_version=app_version,
        source_type=source_type,
        citation_count=citation_count,
        verified_count=verified_count,
        not_found_count=not_found_count,
        ambiguous_count=ambiguous_count,
        derived_count=derived_count,
        error_count=error_count,
        unverified_no_token_count=unverified_no_token_count,
        had_warning=had_warning,
        latency_ms=latency_ms,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event
