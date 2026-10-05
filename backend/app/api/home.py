"""App shell pages. Only "Needs you" shows real data in Phase 0; the rest are empty states."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_role
from app.core.db import get_db
from app.core.models import ReviewItem, User
from app.core.web import render

router = APIRouter()

REVIEW_TYPE_LABELS = {
    "shortlist_ready": ("Shortlist ready", "pill-info"),
    "possible_duplicate": ("Possible duplicate", "pill-warn"),
    "low_confidence": ("Low confidence", "pill-warn"),
    "file_unreadable": ("File could not be read", "pill-bad"),
    "request_needs_info": ("Needs information", "pill-warn"),
    "job_failed": ("Task failed", "pill-bad"),
}

# section key -> (title, empty-state text for the list pane)
SECTIONS = {
    "requests": (
        "Hiring requests",
        "No hiring requests yet. When a company submits a request, it will appear here with its shortlist.",
    ),
    "candidates": (
        "Candidates",
        "No candidates yet. Once CVs are read, every person will appear here with one profile.",
    ),
    "activity": (
        "Activity",
        "Nothing has happened yet. Every action by a person or an agent will be listed here.",
    ),
    "reports": (
        "Reports",
        "No data to report yet. Quarterly numbers will appear here once requests come in.",
    ),
    "tickets": (
        "Tickets",
        "No tickets. When someone flags a problem, it will appear here with the logs attached.",
    ),
    "settings": (
        "Settings",
        "Users and the retention setting will be managed here.",
    ),
}


@router.get("/needs-you")
def needs_you(
    request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> Response:
    items = db.scalars(
        select(ReviewItem).where(ReviewItem.status == "open").order_by(ReviewItem.created_at.desc()).limit(100)
    ).all()
    return render(
        request,
        "needs_you.html",
        {"items": items, "type_labels": REVIEW_TYPE_LABELS},
        user=user,
        db=db,
        section="needs-you",
    )


def _section_page(request: Request, key: str, user: User, db: Session) -> Response:
    title, empty = SECTIONS[key]
    return render(
        request,
        "section.html",
        {"title": title, "empty_text": empty},
        user=user,
        db=db,
        section=key,
    )


@router.get("/requests")
def requests_page(
    request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> Response:
    return _section_page(request, "requests", user, db)


@router.get("/activity")
def activity_page(
    request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> Response:
    return _section_page(request, "activity", user, db)


@router.get("/reports")
def reports_page(
    request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> Response:
    return _section_page(request, "reports", user, db)


@router.get("/tickets")
def tickets_page(
    request: Request, user: User = Depends(require_role("engineer")), db: Session = Depends(get_db)
) -> Response:
    return _section_page(request, "tickets", user, db)


@router.get("/settings")
def settings_page(
    request: Request, user: User = Depends(require_role("hr_admin")), db: Session = Depends(get_db)
) -> Response:
    return _section_page(request, "settings", user, db)
