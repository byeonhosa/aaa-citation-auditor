import logging

import httpx
from fastapi import APIRouter
from sqlalchemy import text

from aaa_db.session import SessionLocal
from app.settings import settings

router = APIRouter(prefix="/api", tags=["api"])

logger = logging.getLogger(__name__)


@router.get("/health")
def health() -> dict[str, str]:
    db_status = "connected"
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
    except Exception:
        logger.exception("Health check: database not reachable")
        db_status = "error"

    cl_status = "reachable"
    try:
        resp = httpx.head(settings.verification_base_url, timeout=5.0)
        if resp.status_code >= 500:
            cl_status = "unreachable"
    except Exception:
        cl_status = "unreachable"

    overall = "ok" if db_status == "connected" and cl_status == "reachable" else "degraded"
    return {"status": overall, "database": db_status, "courtlistener": cl_status}
