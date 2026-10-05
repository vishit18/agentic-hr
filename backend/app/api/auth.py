"""Login and logout."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.audit import audit, hash_input
from app.core.auth import (
    burn_password_check,
    get_current_user,
    get_current_user_optional,
    login_session,
    logout_session,
    verify_password,
)
from app.core.db import get_db
from app.core.models import User
from app.core.ratelimit import FailureLimiter
from app.core.web import render

router = APIRouter()
limiter = FailureLimiter(settings.login_window_seconds)

BAD_LOGIN = "Email or password is incorrect."
TOO_MANY = "Too many failed attempts. Please wait a few minutes and try again."


@router.get("/login")
def login_form(request: Request, user: User | None = Depends(get_current_user_optional)) -> Response:
    if user is not None:
        return RedirectResponse("/needs-you", status_code=303)
    return render(request, "login.html")


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    email = email.strip().lower()[:320]
    ip_key = f"ip:{request.client.host if request.client else 'unknown'}"
    email_key = f"email:{email}"

    if limiter.blocked(ip_key, settings.login_max_failures_per_ip) or limiter.blocked(
        email_key, settings.login_max_failures_per_email
    ):
        audit(db, actor_type="system", action="login.blocked", input=email)
        db.commit()
        return render(request, "login.html", {"error": TOO_MANY, "email": email}, status_code=429)

    user = db.scalar(select(User).where(User.email == email)) if email else None
    if user is not None and user.is_active and verify_password(user.password_hash, password):
        limiter.reset(email_key)
        audit(
            db,
            actor_type="user",
            actor_id=user.id,
            actor_name=user.email,
            action="login.ok",
            entity_type="user",
            entity_id=user.id,
        )
        db.commit()
        response = RedirectResponse("/needs-you", status_code=303)
        login_session(response, user.id)
        return response

    if user is None:
        burn_password_check(password)
    limiter.record(ip_key)
    limiter.record(email_key)
    audit(db, actor_type="system", action="login.failed", input=email, output={"email_hash": hash_input(email)})
    db.commit()
    return render(request, "login.html", {"error": BAD_LOGIN, "email": email}, status_code=401)


@router.post("/logout")
def logout(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Response:
    audit(db, actor_type="user", actor_id=user.id, actor_name=user.email, action="logout")
    db.commit()
    response = RedirectResponse("/login", status_code=303)
    logout_session(response)
    return response
