"""Passwords (argon2), signed session cookie, current-user and role dependencies.

The session cookie always exists (even before login) because it carries the CSRF token.
Payload: {"csrf": str, "uid": int | None}.
"""
from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app.config import settings
from app.core.db import get_db
from app.core.models import User

_hasher = PasswordHasher()
_serializer = URLSafeTimedSerializer(settings.app_secret_key, salt="cadence-session")


class NotAuthenticated(Exception):
    """Raised when a route needs a logged-in user. Handled in main.py (redirect or 401)."""


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


# Used to spend the same time when the email is unknown (no user enumeration by timing).
_DUMMY_HASH = _hasher.hash("not-a-real-password")


def burn_password_check(password: str) -> None:
    verify_password(_DUMMY_HASH, password)


def read_session(request: Request) -> dict[str, Any]:
    token = request.cookies.get(settings.session_cookie)
    if token:
        try:
            data = _serializer.loads(token, max_age=settings.session_max_age_seconds)
            if isinstance(data, dict) and isinstance(data.get("csrf"), str):
                return data
        except BadSignature:
            pass
    return {"csrf": secrets.token_urlsafe(32), "uid": None, "_new": True}


def set_session(response: Response, data: dict[str, Any]) -> None:
    clean = {"csrf": data["csrf"], "uid": data.get("uid")}
    response.set_cookie(
        settings.session_cookie,
        _serializer.dumps(clean),
        max_age=settings.session_max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.app_env != "local",
        path="/",
    )


def login_session(response: Response, user_id: int) -> None:
    """Start an authenticated session with a fresh CSRF token (rotated on login)."""
    set_session(response, {"csrf": secrets.token_urlsafe(32), "uid": user_id})


def logout_session(response: Response) -> None:
    set_session(response, {"csrf": secrets.token_urlsafe(32), "uid": None})


def get_current_user_optional(request: Request, db: Session = Depends(get_db)) -> User | None:
    uid = read_session(request).get("uid")
    if not uid:
        return None
    user = db.get(User, uid)
    if user is None or not user.is_active:
        return None
    return user


def get_current_user(user: User | None = Depends(get_current_user_optional)) -> User:
    if user is None:
        raise NotAuthenticated()
    return user


def require_role(*roles: str) -> Callable[..., User]:
    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="You do not have access to this page.")
        return user

    return dependency
