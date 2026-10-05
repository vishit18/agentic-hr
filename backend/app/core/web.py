"""Template rendering shared by all pages: user, CSRF token, nav, counts."""
from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.responses import Response

from app.config import settings
from app.core.auth import read_session, set_session
from app.core.models import Job, ReviewItem, User

templates = Jinja2Templates(directory=str(settings.frontend_dir / "templates"))

# key, label, href, roles allowed (None = everyone logged in), shown in the mobile tab bar
NAV: list[dict[str, Any]] = [
    {"key": "needs-you", "label": "Needs you", "href": "/needs-you", "roles": None, "tab": True},
    {"key": "requests", "label": "Hiring requests", "href": "/requests", "roles": None, "tab": True},
    {"key": "candidates", "label": "Candidates", "href": "/candidates", "roles": None, "tab": True},
    {"key": "activity", "label": "Activity", "href": "/activity", "roles": None, "tab": True},
    {"key": "reports", "label": "Reports", "href": "/reports", "roles": None, "tab": True},
    {"key": "tickets", "label": "Tickets", "href": "/tickets", "roles": ("engineer",), "tab": False},
    {"key": "settings", "label": "Settings", "href": "/settings", "roles": ("hr_admin",), "tab": False},
]


def nav_for(user: User) -> list[dict[str, Any]]:
    return [n for n in NAV if n["roles"] is None or user.role in n["roles"]]


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    user: User | None = None,
    db: Session | None = None,
    section: str | None = None,
    status_code: int = 200,
) -> Response:
    """Render a template with the CSRF token and (for logged-in pages) the app shell data."""
    session = read_session(request)
    ctx: dict[str, Any] = {"csrf_token": session["csrf"], "user": user, "section": section}
    if user is not None:
        ctx["nav"] = nav_for(user)
        if db is not None:
            ctx["needs_you_count"] = db.scalar(
                select(func.count()).select_from(ReviewItem).where(ReviewItem.status == "open")
            )
            ctx["agents_running"] = db.scalar(
                select(func.count()).select_from(Job).where(Job.status == "running")
            )
    ctx.update(context or {})
    response = templates.TemplateResponse(request, name, ctx, status_code=status_code)
    if session.get("_new"):
        set_session(response, session)
    return response
