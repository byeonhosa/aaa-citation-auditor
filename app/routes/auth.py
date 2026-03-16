"""Authentication routes: register, login, logout."""

from __future__ import annotations

import logging
from contextlib import contextmanager

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from aaa_db.session import SessionLocal
from app.services.auth import authenticate_user, create_user, get_user_by_email
from app.settings import TEMPLATES_DIR

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@contextmanager
def _db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _set_session(request: Request, user) -> None:  # noqa: ANN001
    request.session["user_id"] = user.id
    request.session["user_email"] = user.email
    request.session["user_name"] = user.name


@router.get("/register", response_class=HTMLResponse)
def register_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={"title": "Register"},
    )


@router.post("/register", response_class=HTMLResponse, response_model=None)
async def register_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    name: str = Form(default=""),
) -> HTMLResponse | RedirectResponse:
    errors: list[str] = []
    email = email.strip().lower()
    name = name.strip()

    if not email:
        errors.append("Email is required.")
    if len(password) < 8:
        errors.append("Password must be at least 8 characters.")

    if errors:
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={"title": "Register", "errors": errors, "email": email, "name": name},
        )

    with _db() as db:
        if get_user_by_email(db, email):
            errors.append("An account with this email already exists.")
            return templates.TemplateResponse(
                request=request,
                name="register.html",
                context={"title": "Register", "errors": errors, "email": email, "name": name},
            )
        user = create_user(db, email=email, password=password, name=name)

    _set_session(request, user)
    return RedirectResponse(url="/", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"title": "Login"},
    )


@router.post("/login", response_class=HTMLResponse, response_model=None)
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
) -> HTMLResponse | RedirectResponse:
    email = email.strip().lower()
    with _db() as db:
        user = authenticate_user(db, email, password)

    if user is None:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"title": "Login", "error": "Invalid email or password.", "email": email},
        )

    _set_session(request, user)
    logger.info("User logged in: %s", user.email)
    return RedirectResponse(url="/", status_code=303)


@router.get("/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
