import logging
import sys

from alembic.config import Config as AlembicConfig
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse

from aaa_db.session import SessionLocal
from aaa_db.telemetry_repository import get_or_create_install_id, record_telemetry_event
from alembic import command as alembic_command
from app.routes.api import router as api_router
from app.routes.auth import router as auth_router
from app.routes.pages import router as pages_router
from app.services.auth import users_exist
from app.services.notifications import validate_email_config
from app.settings import PROJECT_ROOT, STATIC_DIR, TEMPLATES_DIR, settings

logger = logging.getLogger(__name__)

# Paths that never require authentication.
_PUBLIC_PATHS = frozenset(
    {
        "/",
        "/login",
        "/register",
        "/logout",
        "/api/health",
        "/waitlist",
        "/about",
        "/contact",
        "/privacy",
    }
)


class AuthMiddleware(BaseHTTPMiddleware):
    """Redirect unauthenticated requests to /login.

    Only active when at least one user exists in the database (backward-
    compatible with empty deployments).  Static assets and the auth routes
    themselves are always allowed through.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Always allow auth pages and static assets.
        if path in _PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)

        # No auth enforcement until at least one user is registered.
        with SessionLocal() as db:
            if not users_exist(db):
                return await call_next(request)

        if request.session.get("user_id") is None:
            return RedirectResponse(url="/login", status_code=303)

        return await call_next(request)


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        stream=sys.stdout,
        force=True,
    )


def _run_migrations() -> None:
    """Apply any pending Alembic migrations on startup."""
    alembic_cfg = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    alembic_command.upgrade(alembic_cfg, "head")


def create_app() -> FastAPI:
    configure_logging(settings.log_level)
    if not settings.govinfo_api_key:
        logger.warning("GOVINFO_API_KEY not configured — federal statute verification disabled")
    # Outbound email is mandatory in production. Refuse to start if the
    # Resend key is missing rather than silently dropping every alert.
    validate_email_config()
    logger.info(
        "Starting %s (version %s, log_level=%s)",
        settings.app_name,
        settings.app_version,
        settings.log_level,
    )

    app = FastAPI(title=settings.app_name, debug=settings.debug)

    # Middleware ordering: the LAST add_middleware call becomes the OUTERMOST
    # wrapper (runs first on every request).  We need SessionMiddleware to run
    # before AuthMiddleware so that request.session is populated when the auth
    # check executes.
    #
    # Desired request-processing order:
    #   SessionMiddleware → AuthMiddleware → route handler
    #
    # To achieve this, add AuthMiddleware first (ends up inner) then
    # SessionMiddleware second (ends up outer):
    app.add_middleware(AuthMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        max_age=86400,  # 24 hours
    )

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Apply pending database migrations (replaces Base.metadata.create_all).
    _run_migrations()

    app.include_router(auth_router)
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
        logger.exception("Failed to record startup telemetry.")

    return app


app = create_app()
