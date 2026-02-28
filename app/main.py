from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from aaa_db.models import Base
from aaa_db.session import engine
from app.routes.api import router as api_router
from app.routes.pages import router as pages_router
from app.settings import STATIC_DIR, settings


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, debug=settings.debug)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    Base.metadata.create_all(bind=engine)

    app.include_router(pages_router)
    app.include_router(api_router)
    return app


app = create_app()
