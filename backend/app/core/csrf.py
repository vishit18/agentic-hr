"""CSRF protection for every unsafe request (forms and HTMX).

The token lives in the signed session cookie. Clients send it as the X-CSRF-Token header
(HTMX, set in app.js from the "csrf-token" meta tag) or as a hidden form field "csrf_token".
"""
from __future__ import annotations

import hmac

from fastapi import HTTPException, Request

from app.config import settings
from app.core.auth import read_session

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


async def verify_csrf(request: Request) -> None:
    if request.method in SAFE_METHODS:
        return
    expected = read_session(request)["csrf"]
    supplied = request.headers.get("x-csrf-token")
    if not supplied:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            form = await request.form()
            value = form.get("csrf_token")
            supplied = value if isinstance(value, str) else None
    if (
        request.cookies.get(settings.session_cookie) is None
        or not supplied
        or not hmac.compare_digest(supplied, expected)
    ):
        raise HTTPException(status_code=403, detail="Security check failed. Reload the page and try again.")
