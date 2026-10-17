"""Hiring requests: list, create, review the shortlist, edit requirements, notes and files, approve and send."""
from __future__ import annotations

import smtplib
from datetime import datetime, timedelta, timezone

from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, File as FormFile, Form, HTTPException, Request as HttpRequest, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import queue
from app.core.audit import audit
from app.core.auth import require_staff
from app.core.db import get_db
from app.core.links import make_company_token
from app.core.mail import send_mail
from app.core.models import (
    Attachment,
    AuditLog,
    Candidate,
    CandidateVersion,
    File,
    Note,
    Request,
    RequestRequirement,
    ReviewItem,
    ShortlistItem,
    User,
)
from app.core.uploads import save_upload
from app.core.web import render
from app.matching.scoring import Requirement, score_candidate
from app.matching.search import Filters, search
from app.workflows.hiring_request import RequestInput, create_request, guess_category, requests_dir
from app.workflows.share import company_view, render_html, render_text

router = APIRouter(dependencies=[])
staff = require_staff

STATUS_LABELS = {
    "new": ("New", "pill-info"), "needs_info": ("Needs info", "pill-warn"), "shortlisting": ("Shortlisting", "pill-info"),
    "ready_for_review": ("Ready for review", "pill-info"), "shared": ("Shared", "pill-ok"), "closed": ("Closed", "pill-muted"),
}
CATEGORIES = ["certification", "skill", "job_title", "industry", "years_experience", "education", "language", "other"]
from app.core.labels import ACTION_LABELS  # noqa: E402


def get_request(db: Session, request_id: int) -> Request:
    req = db.get(Request, request_id)
    if req is None:
        raise HTTPException(status_code=404)
    return req


def _log(db: Session, user: User, action: str, req: Request, output: dict | None = None) -> None:
    audit(db, actor_type="user", actor_id=user.id, actor_name=user.email, action=action,
          entity_type="request", entity_id=req.id, output=output)


def _set_data(req: Request, **kw: object) -> None:
    req.data = {**(req.data or {}), **kw}  # new dict so the JSON column change is detected


def active_items(db: Session, request_id: int) -> list[ShortlistItem]:
    return list(db.scalars(select(ShortlistItem).where(
        ShortlistItem.request_id == request_id, ShortlistItem.status.in_(("suggested", "approved", "shared")))
        .order_by(ShortlistItem.rank)))


def renumber(db: Session, request_id: int) -> None:
    for i, item in enumerate(active_items(db, request_id), start=1):
        item.rank = i


def _redirect(request_id: int, anchor: str = "", **params: str) -> RedirectResponse:
    qs = ("?" + "&".join(f"{k}={v}" for k, v in params.items())) if params else ""
    return RedirectResponse(f"/requests/{request_id}{qs}{anchor}", status_code=303)


# ---------------------------------------------------------------- list, create, links

@router.get("/requests")
def list_requests(request: HttpRequest, company: str = "", owner: str = "", status: str = "", date_from: str = "",
                  date_to: str = "", page: int = 1, user: User = Depends(staff), db: Session = Depends(get_db)) -> Response:
    stmt = select(Request)
    if company.strip():
        stmt = stmt.where(Request.company.ilike(f"%{company.strip()[:100]}%"))
    if owner.isdigit():
        stmt = stmt.where(Request.owner_id == int(owner))
    if status in STATUS_LABELS:
        stmt = stmt.where(Request.status == status)
    for value, op in ((date_from, "ge"), (date_to, "le")):
        try:
            d = datetime.fromisoformat(value).replace(tzinfo=timezone.utc) if value else None
        except ValueError:
            d = None
        if d is not None:
            stmt = stmt.where(Request.received_at >= d if op == "ge" else Request.received_at < d + timedelta(days=1))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    size = 25
    rows = db.scalars(stmt.order_by(Request.received_at.desc()).limit(size).offset((max(page, 1) - 1) * size)).all()
    owners = {u.id: u for u in db.scalars(select(User))}
    counts = dict(db.execute(select(ShortlistItem.request_id, func.count()).where(
        ShortlistItem.status != "removed").group_by(ShortlistItem.request_id)).all())
    return render(request, "requests/list.html", {
        "rows": rows, "owners": owners, "counts": counts, "total": total, "labels": STATUS_LABELS, "page": page,
        "pages": max((total + size - 1) // size, 1), "staff_users": [u for u in owners.values() if u.role != "engineer"],
        "q": {"company": company, "owner": owner, "status": status, "date_from": date_from, "date_to": date_to},
        "new_link": request.query_params.get("link"),
    }, user=user, db=db, section="requests")


@router.post("/requests/links")
def make_link(request: HttpRequest, company: str = Form(""), user: User = Depends(staff),
              db: Session = Depends(get_db)) -> Response:
    company = company.strip()[:200]
    if not company:
        raise HTTPException(status_code=400, detail="Company name is required")
    audit(db, actor_type="user", actor_id=user.id, actor_name=user.email, action="company_link.created", output={"company": company})
    db.commit()
    link = f"{str(request.base_url).rstrip('/')}/apply/{make_company_token(company)}"
    return RedirectResponse("/requests?link=" + link.replace("?", "%3F"), status_code=303)


@router.get("/requests/new")
def new_form(request: HttpRequest, user: User = Depends(staff), db: Session = Depends(get_db)) -> Response:
    return render(request, "requests/new.html", {"values": {}}, user=user, db=db, section="requests")


@router.post("/requests/new")
async def new_submit(
    request: HttpRequest, company: str = Form(""), contact_name: str = Form(""), contact_email: str = Form(""),
    job_title: str = Form(""), location: str = Form(""), text: str = Form(""), attachment: UploadFile | None = FormFile(None),
    user: User = Depends(staff), db: Session = Depends(get_db),
) -> Response:
    values = dict(company=company, contact_name=contact_name, contact_email=contact_email, job_title=job_title,
                  location=location, text=text)

    def again(msg: str) -> Response:
        return render(request, "requests/new.html", {"values": values, "error": msg}, user=user, db=db,
                      section="requests", status_code=400)

    if not company.strip():
        return again("Please enter the company name.")
    if not text.strip() and not (attachment and attachment.filename):
        return again("Paste the request or upload a job description.")
    file_row = None
    if attachment and attachment.filename:
        saved = await save_upload(attachment, requests_dir())
        if isinstance(saved, str):
            return again(saved)
        path, sha = saved
        file_row = db.scalar(select(File).where(File.sha256 == sha)) or File(
            sha256=sha, original_name=path.name.split("_", 1)[-1], stored_path=str(path),
            size_bytes=path.stat().st_size, origin="upload", status="registered")
        db.add(file_row)
        db.flush()
    req = create_request(db, RequestInput(
        company=company, contact_name=contact_name, contact_email=contact_email, job_title=job_title,
        location=location, description=text[:20000], via="recruiter"), attachment_file=file_row, created_by=user)
    db.commit()
    return _redirect(req.id)


# ---------------------------------------------------------------- detail

def timeline(db: Session, req: Request) -> list[dict]:
    events = [{"ts": a.ts, "who": a.actor_name or a.actor_type, "text": ACTION_LABELS.get(a.action, a.action)}
              for a in db.scalars(select(AuditLog).where(AuditLog.entity_type == "request", AuditLog.entity_id == req.id)
                                  .where(AuditLog.action != "agent.matcher"))]
    return sorted(events, key=lambda e: e["ts"], reverse=True)


@router.get("/requests/{request_id:int}")
def detail(request_id: int, request: HttpRequest, user: User = Depends(staff), db: Session = Depends(get_db)) -> Response:
    req = get_request(db, request_id)
    items = active_items(db, req.id)
    removed = list(db.scalars(select(ShortlistItem).where(ShortlistItem.request_id == req.id, ShortlistItem.status == "removed")))
    cand_ids = [i.candidate_id for i in items + removed]
    versions = {v.candidate_id: v for v in db.scalars(
        select(CandidateVersion).join(Candidate, Candidate.current_version_id == CandidateVersion.id)
        .where(Candidate.id.in_(cand_ids or [-1])))}
    reqs = list(db.scalars(select(RequestRequirement).where(RequestRequirement.request_id == req.id).order_by(RequestRequirement.id)))
    notes = list(db.scalars(select(Note).where(Note.entity_type == "request", Note.entity_id == req.id).order_by(Note.created_at.desc())))
    atts = list(db.scalars(select(Attachment).where(Attachment.request_id == req.id)))
    users = {u.id: u for u in db.scalars(select(User))}
    needs_item = db.scalar(select(ReviewItem).where(ReviewItem.type == "request_needs_info", ReviewItem.entity_id == req.id,
                                                    ReviewItem.status == "open").order_by(ReviewItem.id.desc()))
    return render(request, "requests/detail.html", {
        "needs_item": needs_item,
        "r": req, "items": items, "removed": removed, "versions": versions, "reqs": reqs, "notes": notes, "atts": atts,
        "users": users, "staff_users": [u for u in users.values() if u.role != "engineer"], "labels": STATUS_LABELS,
        "categories": CATEGORIES, "timeline": timeline(db, req), "summary": (req.data or {}).get("match_summary") or {},
        "sent": request.query_params.get("sent"), "undo": request.query_params.get("undo"),
        "error": request.query_params.get("error"),
    }, user=user, db=db, section="requests")


@router.get("/requests/{request_id:int}/status-poll")
def status_poll(request_id: int, user: User = Depends(staff), db: Session = Depends(get_db)) -> Response:
    """Polled while the shortlist is being built; tells the page to reload when it is ready."""
    req = get_request(db, request_id)
    if req.status in ("new", "shortlisting"):
        return Response("Working on the shortlist…", media_type="text/plain")
    return Response("", headers={"HX-Refresh": "true"})


# ---------------------------------------------------------------- owner and requirements

@router.post("/requests/{request_id:int}/owner")
def set_owner(request_id: int, user_id: int = Form(...), user: User = Depends(staff), db: Session = Depends(get_db)) -> Response:
    req = get_request(db, request_id)
    new = db.get(User, user_id)
    if new is None or new.role == "engineer" or not new.is_active:
        raise HTTPException(status_code=400, detail="Choose a recruiter")
    old = req.owner_id
    req.owner_id = new.id
    _log(db, user, "request.owner_changed", req, {"from": old, "to": new.id})
    db.commit()
    return _redirect(req.id)


@router.post("/requests/{request_id:int}/requirements")
def add_requirement(request_id: int, kind: str = Form("must"), category: str = Form("other"), text: str = Form(""),
                    user: User = Depends(staff), db: Session = Depends(get_db)) -> Response:
    req = get_request(db, request_id)
    text = text.strip()[:500]
    if not text:
        return _redirect(req.id, "#requirements", error="Enter+the+requirement")
    kind = kind if kind in ("must", "nice") else "must"
    category = category if category in CATEGORIES else guess_category(text)
    db.add(RequestRequirement(request_id=req.id, kind=kind, category=category, text=text, value={}, edited_by=user.id))
    _set_data(req, needs_rematch=True, human_edited=True)
    _log(db, user, "request.requirement_added", req, {"text": text, "kind": kind})
    db.commit()
    return _redirect(req.id, "#requirements")


@router.post("/requests/{request_id:int}/requirements/{req_id:int}/update")
def edit_requirement(request_id: int, req_id: int, kind: str = Form("must"), category: str = Form("other"),
                     text: str = Form(""), user: User = Depends(staff), db: Session = Depends(get_db)) -> Response:
    req = get_request(db, request_id)
    row = db.get(RequestRequirement, req_id)
    if row is None or row.request_id != req.id:
        raise HTTPException(status_code=404)
    text = text.strip()[:500]
    if text:
        row.text, row.edited_by = text, user.id
        row.kind = kind if kind in ("must", "nice") else row.kind
        row.category = category if category in CATEGORIES + ["nationality"] else row.category
        _set_data(req, needs_rematch=True, human_edited=True)
        _log(db, user, "request.requirement_edited", req, {"id": row.id, "text": text})
        db.commit()
    return _redirect(req.id, "#requirements")


@router.post("/requests/{request_id:int}/requirements/{req_id:int}/delete")
def delete_requirement(request_id: int, req_id: int, user: User = Depends(staff), db: Session = Depends(get_db)) -> Response:
    req = get_request(db, request_id)
    row = db.get(RequestRequirement, req_id)
    if row is None or row.request_id != req.id:
        raise HTTPException(status_code=404)
    _log(db, user, "request.requirement_removed", req, {"text": row.text})
    db.delete(row)
    _set_data(req, needs_rematch=True, human_edited=True)  # a later model re-run must not re-add what a person removed
    db.commit()
    return _redirect(req.id, "#requirements")


@router.post("/requests/{request_id:int}/rematch")
def rematch(request_id: int, user: User = Depends(staff), db: Session = Depends(get_db)) -> Response:
    req = get_request(db, request_id)
    req.status = "shortlisting"
    _set_data(req, needs_rematch=False)
    queue.enqueue(db, "request.rematch", {"request_id": req.id})
    _log(db, user, "request.rematch", req)
    db.commit()
    return _redirect(req.id)


# ---------------------------------------------------------------- shortlist actions

def _item(db: Session, req: Request, item_id: int) -> ShortlistItem:
    item = db.get(ShortlistItem, item_id)
    if item is None or item.request_id != req.id:
        raise HTTPException(status_code=404)
    return item


@router.post("/requests/{request_id:int}/shortlist/{item_id:int}/move")
def move(request_id: int, item_id: int, direction: str = Form("up"), user: User = Depends(staff),
         db: Session = Depends(get_db)) -> Response:
    req = get_request(db, request_id)
    item = _item(db, req, item_id)
    items = active_items(db, req.id)
    idx = next((i for i, x in enumerate(items) if x.id == item.id), None)
    swap = None if idx is None else (idx - 1 if direction == "up" else idx + 1)
    if idx is not None and swap is not None and 0 <= swap < len(items):
        items[idx].rank, items[swap].rank = items[swap].rank, items[idx].rank
        _log(db, user, "request.candidate_moved", req, {"candidate_id": item.candidate_id, "direction": direction})
        db.commit()
    return _redirect(req.id, f"#item-{item.id}")


@router.post("/requests/{request_id:int}/shortlist/{item_id:int}/remove")
def remove(request_id: int, item_id: int, user: User = Depends(staff), db: Session = Depends(get_db)) -> Response:
    req = get_request(db, request_id)
    item = _item(db, req, item_id)
    item.status, item.decided_by, item.decided_at = "removed", user.id, datetime.now(timezone.utc)
    renumber(db, req.id)
    _log(db, user, "request.candidate_removed", req, {"candidate_id": item.candidate_id, "agent_rank": item.agent_rank})
    db.commit()
    return _redirect(req.id, "#shortlist", undo=str(item.id))


@router.post("/requests/{request_id:int}/shortlist/{item_id:int}/restore")
def restore(request_id: int, item_id: int, user: User = Depends(staff), db: Session = Depends(get_db)) -> Response:
    req = get_request(db, request_id)
    item = _item(db, req, item_id)
    item.rank = len(active_items(db, req.id)) + 1  # counted before it rejoins the list
    item.status = "suggested"
    _log(db, user, "request.candidate_restored", req, {"candidate_id": item.candidate_id})
    db.commit()
    return _redirect(req.id, "#shortlist")


@router.get("/requests/{request_id:int}/add")
def add_search(request_id: int, request: HttpRequest, q: str = "", user: User = Depends(staff),
               db: Session = Depends(get_db)) -> Response:
    req = get_request(db, request_id)
    result = search(db, Filters(q=q[:200])) if q.strip() else None
    return render(request, "requests/add.html", {"r": req, "q": q, "result": result}, user=user, db=db, section="requests")


@router.post("/requests/{request_id:int}/shortlist/add")
def add_candidate(request_id: int, candidate_id: int = Form(...), user: User = Depends(staff),
                  db: Session = Depends(get_db)) -> Response:
    req = get_request(db, request_id)
    cand = db.get(Candidate, candidate_id)
    version = db.get(CandidateVersion, cand.current_version_id) if cand and cand.current_version_id else None
    if cand is None or version is None or cand.merged_into_id:
        raise HTTPException(status_code=404)
    existing = db.scalar(select(ShortlistItem).where(ShortlistItem.request_id == req.id, ShortlistItem.candidate_id == cand.id))
    if existing:
        existing.status = "suggested"
        existing.explanation = {**(existing.explanation or {}), "manual": True}
    else:
        reqs = [Requirement(r.id, r.kind, r.category, r.text) for r in db.scalars(
            select(RequestRequirement).where(RequestRequirement.request_id == req.id))]
        s = score_candidate(reqs, version, job_title=req.job_title, min_years=(req.data or {}).get("min_years"), similarity=None)
        db.add(ShortlistItem(
            request_id=req.id, candidate_id=cand.id, rank=len(active_items(db, req.id)) + 1, agent_rank=None, score=s.score,
            score_breakdown={"components": s.components, "requirements": [x.as_dict() for x in s.results], "retrieval": "manual"},
            explanation={"summary": "Added by a recruiter.", "strengths": [], "gaps": [], "source": "manual", "manual": True},
            status="suggested", decided_by=user.id, decided_at=datetime.now(timezone.utc)))
    _log(db, user, "request.candidate_added", req, {"candidate_id": cand.id})
    db.commit()
    return _redirect(req.id, "#shortlist")


# ---------------------------------------------------------------- notes, files, status

@router.post("/requests/{request_id:int}/notes")
def add_note(request_id: int, body: str = Form(""), kind: str = Form("note"), user: User = Depends(staff),
             db: Session = Depends(get_db)) -> Response:
    req = get_request(db, request_id)
    body = body.strip()[:4000]
    if body:
        db.add(Note(entity_type="request", entity_id=req.id, kind="next_step" if kind == "next_step" else "note",
                    body=body, author_id=user.id))
        _log(db, user, "request.note_added", req, {"kind": kind})
        db.commit()
    return _redirect(req.id, "#notes")


@router.post("/requests/{request_id:int}/files")
async def add_file(request_id: int, attachment: UploadFile = FormFile(...), user: User = Depends(staff),
                   db: Session = Depends(get_db)) -> Response:
    req = get_request(db, request_id)
    saved = await save_upload(attachment, requests_dir())
    if isinstance(saved, str):
        return _redirect(req.id, "#files", error=saved.replace(" ", "+"))
    path, sha = saved
    f = db.scalar(select(File).where(File.sha256 == sha)) or File(
        sha256=sha, original_name=path.name.split("_", 1)[-1], stored_path=str(path), size_bytes=path.stat().st_size,
        origin="upload", status="registered")
    db.add(f)
    db.flush()
    db.add(Attachment(request_id=req.id, file_id=f.id, filename=f.original_name, uploaded_by=user.id))
    _log(db, user, "file.uploaded", req, {"name": f.original_name})
    db.commit()
    return _redirect(req.id, "#files")


@router.post("/requests/{request_id:int}/close")
def close(request_id: int, reopen: str = Form(""), user: User = Depends(staff), db: Session = Depends(get_db)) -> Response:
    req = get_request(db, request_id)
    if reopen:
        req.status, req.closed_at = "ready_for_review", None
        _log(db, user, "request.reopened", req)
    else:
        req.status, req.closed_at = "closed", datetime.now(timezone.utc)
        _log(db, user, "request.closed", req)
    db.commit()
    return _redirect(req.id)


# ---------------------------------------------------------------- approve and send

async def _parse_share_form(request: HttpRequest, db: Session, req: Request):
    form = await request.form()
    items = active_items(db, req.id)
    people, chosen = [], []
    for item in items:
        if form.get(f"include_{item.id}") != "1":
            continue
        version = db.scalar(select(CandidateVersion).join(Candidate, Candidate.current_version_id == CandidateVersion.id)
                            .where(Candidate.id == item.candidate_id))
        if version is None:
            continue
        file = db.get(File, version.file_id) if version.file_id else None
        summary = str(form.get(f"summary_{item.id}", ""))[:1500]
        people.append(company_view(item, version, file, summary=summary, include_cv=form.get(f"cv_{item.id}") == "1",
                                   include_contact=form.get(f"contact_{item.id}") == "1"))
        chosen.append(item)
    recipient = str(form.get("recipient", "")).strip()
    subject = str(form.get("subject", "")).strip()[:200] or f"Shortlist for {req.job_title or 'your role'} ({req.reference})"
    return items, chosen, people, recipient, subject


@router.get("/requests/{request_id:int}/approve")
def approve_form(request_id: int, request: HttpRequest, user: User = Depends(staff), db: Session = Depends(get_db)) -> Response:
    req = get_request(db, request_id)
    items = active_items(db, req.id)
    versions = {v.candidate_id: v for v in db.scalars(select(CandidateVersion).join(
        Candidate, Candidate.current_version_id == CandidateVersion.id).where(Candidate.id.in_([i.candidate_id for i in items] or [-1])))}
    return render(request, "requests/approve.html", {
        "r": req, "items": items, "versions": versions, "recipient": req.contact_email or "",
        "subject": f"Shortlist for {req.job_title or 'your role'} ({req.reference})", "preview": None,
        "checked": {i.id: True for i in items}, "summaries": {i.id: (i.explanation or {}).get("summary", "") for i in items},
        "cv_on": {}, "contact_on": {},
    }, user=user, db=db, section="requests")


@router.post("/requests/{request_id:int}/approve")
async def approve_preview(request_id: int, request: HttpRequest, user: User = Depends(staff), db: Session = Depends(get_db)) -> Response:
    req = get_request(db, request_id)
    items, chosen, people, recipient, subject = await _parse_share_form(request, db, req)
    form = await request.form()
    versions = {v.candidate_id: v for v in db.scalars(select(CandidateVersion).join(
        Candidate, Candidate.current_version_id == CandidateVersion.id).where(Candidate.id.in_([i.candidate_id for i in items] or [-1])))}
    error = None
    try:
        recipient = validate_email(recipient, check_deliverability=False, test_environment=True).normalized
    except EmailNotValidError:
        error = "Enter a valid email address for the company contact."
    if not people:
        error = "Tick at least one candidate to share."
    preview = None if error else render_text(req.company or "", req.job_title, req.reference, people, user.name)
    return render(request, "requests/approve.html", {
        "r": req, "items": items, "versions": versions, "recipient": recipient, "subject": subject, "preview": preview,
        "people": people, "error": error,
        "checked": {i.id: form.get(f"include_{i.id}") == "1" for i in items},
        "summaries": {i.id: str(form.get(f"summary_{i.id}", "")) for i in items},
        "cv_on": {i.id: form.get(f"cv_{i.id}") == "1" for i in items},
        "contact_on": {i.id: form.get(f"contact_{i.id}") == "1" for i in items},
    }, user=user, db=db, section="requests", status_code=400 if error else 200)


@router.post("/requests/{request_id:int}/send")
async def send(request_id: int, request: HttpRequest, user: User = Depends(staff), db: Session = Depends(get_db)) -> Response:
    """The only place anything leaves Agentic HR. Needs a named person; goes to Mailpit; logs who decided and what the agent suggested."""
    req = get_request(db, request_id)
    items, chosen, people, recipient, subject = await _parse_share_form(request, db, req)
    try:
        recipient = validate_email(recipient, check_deliverability=False, test_environment=True).normalized
    except EmailNotValidError:
        raise HTTPException(status_code=400, detail="Invalid recipient") from None
    if not people:
        raise HTTPException(status_code=400, detail="No candidates selected")
    text = render_text(req.company or "", req.job_title, req.reference, people, user.name)
    html = render_html(req.company or "", req.job_title, req.reference, people, user.name)
    try:
        send_mail(recipient, subject, text, html, [(p.cv_name, p.cv_path) for p in people if p.cv_name and p.cv_path])
    except (OSError, smtplib.SMTPException):
        return _redirect(req.id, "", error="The+email+could+not+be+sent.+Nothing+was+shared.")
    now = datetime.now(timezone.utc)
    for item in chosen:
        item.status, item.decided_by, item.decided_at = "shared", user.id, now
    req.status = "shared"
    for ri in db.scalars(select(ReviewItem).where(ReviewItem.type == "shortlist_ready", ReviewItem.entity_id == req.id,
                                                  ReviewItem.status == "open")):
        ri.status, ri.resolved_by, ri.resolved_at, ri.resolution = "resolved", user.id, now, "approved_and_sent"
    audit(db, actor_type="user", actor_id=user.id, actor_name=user.email, action="request.shared",
          entity_type="request", entity_id=req.id, output={
              "recipient": recipient, "decided_by": user.email,
              "agent_suggested": [i.candidate_id for i in sorted(items, key=lambda x: x.agent_rank or 999) if i.agent_rank],
              "shared": [{"candidate_id": p.candidate_id, "cv": p.include_cv, "contact": p.include_contact} for p in people],
              "not_shared": [i.candidate_id for i in items if i not in chosen]})
    db.commit()
    return _redirect(req.id, "", sent="1")
