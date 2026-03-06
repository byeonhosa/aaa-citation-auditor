from alembic.config import Config as AlembicConfig
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from aaa_db.session import SessionLocal
from aaa_db.telemetry_repository import get_or_create_install_id, record_telemetry_event
from alembic import command as alembic_command
from app.routes.api import router as api_router
from app.routes.pages import router as pages_router
from app.settings import PROJECT_ROOT, STATIC_DIR, TEMPLATES_DIR, settings


def _run_migrations() -> None:
    """Apply any pending Alembic migrations on startup."""
    alembic_cfg = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    alembic_command.upgrade(alembic_cfg, "head")


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, debug=settings.debug)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Apply pending database migrations (replaces Base.metadata.create_all).
    _run_migrations()

    app.include_router(pages_router)
    app.include_router(api_router)

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    @app.exception_handler(StarletteHTTPException)
    async def html_404_handler(request: Request, exc: StarletteHTTPException) -> HTMLResponse:
        if exc.status_code == 404:
            return templates.TemplateResponse(
                request=request,
                name="404.html",
                context={"title": "Not Found"},
                status_code=404,
            )
        return HTMLResponse(content=exc.detail, status_code=exc.status_code)

    try:
        install_id = get_or_create_install_id()
        with SessionLocal() as db:
            record_telemetry_event(
                db,
                event_type="app_started",
                install_id=install_id,
                app_version=settings.app_version,
            )
    except Exception:
        pass

    return app


app = create_app()
