from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from aaa_db.models import Base, TelemetryEvent
from aaa_db.session import SessionLocal, engine
from aaa_db.telemetry_repository import get_or_create_install_id, record_telemetry_event
from app.routes.api import router as api_router
from app.routes.pages import router as pages_router
from app.settings import STATIC_DIR, TEMPLATES_DIR, settings


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, debug=settings.debug)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Ensure all ORM models are imported/registered before table creation.
    _ = TelemetryEvent
    Base.metadata.create_all(bind=engine)

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
