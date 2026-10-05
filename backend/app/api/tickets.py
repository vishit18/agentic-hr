"""Flag a problem (staff) and the engineer ticket view.

Engineers see tickets and the logs attached to them, nothing else. Reading an agent's raw input is
allowed only for rows attached to the ticket, and each read is itself audited.
"""
from __future__ import annotations

import html

from fastapi import APIRouter, Depends, Form, HTTPException, Request as HttpRequest
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.auth import require_role, require_staff
from app.core.db import get_db
from app.core.models import AuditLog, Ticket, User
from app.core.web import render
from app.workflows.tickets import agent_input_text, create_ticket

router = APIRouter()
engineer = require_role("engineer")
STATUSES = ("open", "in_progress", "fixed")
STATUS_LABELS = {"open": ("Open", "pill-warn"), "in_progress": ("In progress", "pill-info"), "fixed": ("Fixed", "pill-ok")}


# ---------------------------------------------------------------- staff: flag and follow

@router.post("/problems")
def flag_problem(message: str = Form(""), page_url: str = Form(""), user: User = Depends(require_staff),
                 db: Session = Depends(get_db)) -> Response:
    message = message.strip()
    if len(message) < 3:
        return HTMLResponse('<span class="toast-bad">Please say what looks wrong.</span>', status_code=400)
    ticket = create_ticket(db, user, message, page_url if page_url.startswith("/") else None)
    db.commit()
    return HTMLResponse(
        f'<span class="toast-ok">Thank you. Engineering has been told (problem #{ticket.id}) with the recent '
        f'activity attached. <a href="/problems">Follow it here</a>.</span>'
    )


@router.get("/problems")
def my_problems(request: HttpRequest, user: User = Depends(require_staff), db: Session = Depends(get_db)) -> Response:
    rows = list(db.scalars(select(Ticket).where(Ticket.reporter_id == user.id).order_by(Ticket.created_at.desc())))
    return render(request, "tickets/mine.html", {"rows": rows, "labels": STATUS_LABELS}, user=user, db=db, section="reports")


# ---------------------------------------------------------------- engineer

@router.get("/tickets")
def list_tickets(request: HttpRequest, status: str = "", user: User = Depends(engineer), db: Session = Depends(get_db)) -> Response:
    stmt = select(Ticket).order_by(Ticket.created_at.desc())
    if status in STATUSES:
        stmt = stmt.where(Ticket.status == status)
    rows = list(db.scalars(stmt.limit(100)))
    reporters = {u.id: u.name for u in db.scalars(select(User))}
    return render(request, "tickets/list.html", {"rows": rows, "labels": STATUS_LABELS, "status": status,
                                                 "reporters": reporters}, user=user, db=db, section="tickets")


def _ticket(db: Session, ticket_id: int) -> Ticket:
    t = db.get(Ticket, ticket_id)
    if t is None:
        raise HTTPException(status_code=404)
    return t


@router.get("/tickets/{ticket_id:int}")
def ticket_detail(ticket_id: int, request: HttpRequest, user: User = Depends(engineer), db: Session = Depends(get_db)) -> Response:
    t = _ticket(db, ticket_id)
    reporter = db.get(User, t.reporter_id)
    return render(request, "tickets/detail.html", {"t": t, "reporter": reporter, "labels": STATUS_LABELS,
                                                   "logs": t.logs or {}, "saved": request.query_params.get("saved")},
                  user=user, db=db, section="tickets")


@router.post("/tickets/{ticket_id:int}")
def update_ticket(ticket_id: int, status: str = Form("open"), fix_note: str = Form(""), user: User = Depends(engineer),
                  db: Session = Depends(get_db)) -> Response:
    t = _ticket(db, ticket_id)
    if status not in STATUSES:
        raise HTTPException(status_code=400)
    t.status, t.fix_note, t.assignee_id = status, fix_note.strip()[:2000] or None, user.id
    audit(db, actor_type="user", actor_id=user.id, actor_name=user.email, action="ticket.updated",
          entity_type="ticket", entity_id=t.id, output={"status": status, "has_fix_note": bool(t.fix_note)})
    db.commit()
    return RedirectResponse(f"/tickets/{t.id}?saved=1", status_code=303)


@router.get("/tickets/{ticket_id:int}/input/{audit_id:int}")
def agent_input(ticket_id: int, audit_id: int, user: User = Depends(engineer), db: Session = Depends(get_db)) -> Response:
    """Raw input of one agent step. Only for rows attached to this ticket; the access is logged."""
    t = _ticket(db, ticket_id)
    attached = {r["id"] for r in (t.logs or {}).get("audit", [])}
    row = db.get(AuditLog, audit_id)
    if row is None or audit_id not in attached:
        raise HTTPException(status_code=404)
    text = agent_input_text(db, row)
    audit(db, actor_type="user", actor_id=user.id, actor_name=user.email, action="agent_input.viewed",
          entity_type="ticket", entity_id=t.id, output={"audit_id": audit_id, "available": text is not None})
    db.commit()
    body = html.escape(text) if text is not None else "The raw input was not stored. Only its hash is kept: " + html.escape(row.input_hash or "none")
    return HTMLResponse(f"<pre class=\"mail-preview\">{body}</pre>", headers={"X-Content-Type-Options": "nosniff"})
