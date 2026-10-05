"""Needs you: everything waiting on a person, with the action buttons. Also merge, undo and unlink."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request as HttpRequest
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.auth import require_staff
from app.core.db import get_db
from app.core.models import Candidate, CandidateVersion, File, Job, ReviewItem, User
from app.core.web import render
from app.workflows.merge import MergeError, merge_candidates, undo_merge, undo_unlink, unlink_version

router = APIRouter()

TYPE_ORDER = ["shortlist_ready", "possible_duplicate", "incomplete_extraction", "low_confidence",
              "file_unreadable", "request_needs_info", "job_failed"]
TYPE_LABELS = {
    "shortlist_ready": ("Shortlist ready", "pill-info"),
    "possible_duplicate": ("Possible duplicate", "pill-warn"),
    "incomplete_extraction": ("Possibly incomplete", "pill-warn"),
    "low_confidence": ("Low confidence", "pill-warn"),
    "file_unreadable": ("File could not be read", "pill-bad"),
    "request_needs_info": ("Needs information", "pill-warn"),
    "job_failed": ("Task failed", "pill-bad"),
}
GROUP_TITLES = {
    "shortlist_ready": "Shortlists ready for approval", "possible_duplicate": "Possible duplicates",
    "incomplete_extraction": "Long CVs that may be missing details", "low_confidence": "Low-confidence extractions",
    "file_unreadable": "Files that could not be read", "request_needs_info": "Requests missing information",
    "job_failed": "Background tasks that failed",
}


def _open_items(db: Session) -> list[ReviewItem]:
    items = list(db.scalars(select(ReviewItem).where(ReviewItem.status == "open").order_by(ReviewItem.created_at.desc())))
    rank = {t: i for i, t in enumerate(TYPE_ORDER)}
    return sorted(items, key=lambda i: rank.get(i.type, 99))  # stable: newest first inside each type


def _profile(db: Session, candidate_id: int | None) -> tuple[Candidate | None, CandidateVersion | None]:
    cand = db.get(Candidate, candidate_id) if candidate_id else None
    ver = db.get(CandidateVersion, cand.current_version_id) if cand and cand.current_version_id else None
    return cand, ver


def compare(a: CandidateVersion | None, b: CandidateVersion | None) -> list[dict[str, Any]]:
    """Side-by-side rows. 'same' marks matches (green), 'conflict' marks both-present-but-different (amber)."""
    da, db_ = (a.data if a else {}) or {}, (b.data if b else {}) or {}

    def row(label: str, left: list[str], right: list[str]) -> dict[str, Any]:
        ls, rs = {x.lower() for x in left}, {x.lower() for x in right}
        shared = ls & rs
        state = "same" if shared else ("conflict" if ls and rs else "")
        return {"label": label, "left": left, "right": right, "state": state, "shared": sorted(shared)}

    def jobs(d: dict) -> list[str]:
        return [f"{e.get('title') or '?'} at {e.get('company') or '?'} ({(e.get('start') or '?')[:4]}-{(e.get('end') or 'now')[:4]})"
                for e in d.get("experience") or []][:6]

    def companies(d: dict) -> list[str]:
        return [e["company"] for e in d.get("experience") or [] if e.get("company")]

    rows = [
        row("Name", [a.full_name] if a and a.full_name else [], [b.full_name] if b and b.full_name else []),
        row("Emails", da.get("emails", []), db_.get("emails", [])),
        row("Phones", da.get("phones", []), db_.get("phones", [])),
        row("Employers", companies(da), companies(db_)),
        row("Schools", [e["institution"] for e in da.get("education", []) if e.get("institution")],
            [e["institution"] for e in db_.get("education", []) if e.get("institution")]),
        row("Certificates", da.get("certifications", []), db_.get("certifications", [])),
    ]
    jobs_row = {"label": "Recent jobs", "left": jobs(da), "right": jobs(db_), "state": "", "shared": []}
    return rows + [jobs_row]


def _detail_context(db: Session, item: ReviewItem) -> dict[str, Any]:
    ctx: dict[str, Any] = {}
    payload = item.payload or {}
    if item.type == "possible_duplicate":
        incoming_id = payload.get("incoming_candidate_id") or item.entity_id
        existing_ids = [i for i in payload.get("existing_candidate_ids") or [] if i and i != incoming_id]
        inc_c, inc_v = _profile(db, incoming_id)
        ex_c, ex_v = _profile(db, existing_ids[0] if existing_ids else None)
        ctx.update(incoming=inc_c, incoming_v=inc_v, existing=ex_c, existing_v=ex_v, rows=compare(ex_v, inc_v),
                   evidence=payload.get("evidence") or [], suggestion=payload.get("suggestion"),
                   merged_already=bool(inc_c and inc_c.merged_into_id) or bool(ex_c and ex_c.merged_into_id),
                   others=existing_ids[1:])
    elif item.type in ("low_confidence", "incomplete_extraction"):
        cand, ver = _profile(db, item.entity_id)
        ctx.update(candidate=cand, version=ver)
    elif item.type == "file_unreadable":
        ctx["file"] = db.get(File, item.entity_id) if item.entity_id else None
    elif item.type == "job_failed":
        ctx["job"] = db.get(Job, item.entity_id) if item.entity_id else None
    return ctx


def _page(request: HttpRequest, db: Session, user: User, selected: ReviewItem | None, **extra: Any) -> Response:
    items = _open_items(db)
    groups: list[tuple[str, list[ReviewItem]]] = []
    for t in TYPE_ORDER:
        rows = [i for i in items if i.type == t]
        if rows:
            groups.append((t, rows))
    owners = {u.id: u.name for u in db.scalars(select(User))}
    ctx = {"groups": groups, "type_labels": TYPE_LABELS, "group_titles": GROUP_TITLES, "selected": selected,
           "owners": owners, "total": len(items), **extra}
    if selected is not None:
        ctx.update(_detail_context(db, selected))
    return render(request, "needs_you.html", ctx, user=user, db=db, section="needs-you")


@router.get("/needs-you")
def needs_you(request: HttpRequest, user: User = Depends(require_staff), db: Session = Depends(get_db)) -> Response:
    return _page(request, db, user, None)


@router.get("/needs-you/{item_id:int}")
def needs_you_item(item_id: int, request: HttpRequest, user: User = Depends(require_staff),
                   db: Session = Depends(get_db)) -> Response:
    item = db.get(ReviewItem, item_id)
    if item is None:
        raise HTTPException(status_code=404)
    return _page(request, db, user, item, error=request.query_params.get("error"))


def _resolve(db: Session, item: ReviewItem, user: User, resolution: str) -> None:
    item.status, item.resolved_by, item.resolved_at, item.resolution = "resolved", user.id, datetime.now(timezone.utc), resolution
    audit(db, actor_type="user", actor_id=user.id, actor_name=user.email, action="review.resolved",
          entity_type="review_item", entity_id=item.id, output={"type": item.type, "resolution": resolution,
                                                                "entity_type": item.entity_type, "entity_id": item.entity_id,
                                                                "agent_suggestion": (item.payload or {}).get("suggestion")})


@router.post("/needs-you/{item_id:int}/resolve")
def resolve_item(item_id: int, resolution: str = Form("handled"), user: User = Depends(require_staff),
                 db: Session = Depends(get_db)) -> Response:
    item = db.get(ReviewItem, item_id)
    if item is None:
        raise HTTPException(status_code=404)
    if resolution not in {"handled", "different_people", "checked_ok", "dismissed"}:
        raise HTTPException(status_code=400)
    if item.status == "open":
        _resolve(db, item, user, resolution)
        db.commit()
    return RedirectResponse("/needs-you", status_code=303)


@router.post("/needs-you/{item_id:int}/retry")
def retry_job(item_id: int, user: User = Depends(require_staff), db: Session = Depends(get_db)) -> Response:
    item = db.get(ReviewItem, item_id)
    job = db.get(Job, item.entity_id) if item and item.entity_type == "job" and item.entity_id else None
    if item is None or job is None:
        raise HTTPException(status_code=404)
    job.status, job.attempts, job.last_error = "queued", 0, None
    job.run_after, job.finished_at = datetime.now(timezone.utc), None
    _resolve(db, item, user, "retried")
    db.commit()
    return RedirectResponse("/needs-you", status_code=303)


# ---------------------------------------------------------------- merge, undo, unlink

@router.get("/merge")
def merge_confirm(request: HttpRequest, keep: int, drop: int, item: int | None = None,
                  user: User = Depends(require_staff), db: Session = Depends(get_db)) -> Response:
    (kc, kv), (dc, dv) = _profile(db, keep), _profile(db, drop)
    if kc is None or dc is None or keep == drop:
        raise HTTPException(status_code=404)
    return render(request, "merge_confirm.html", {"keep": kc, "keep_v": kv, "drop": dc, "drop_v": dv,
                                                   "rows": compare(kv, dv), "item_id": item,
                                                   "error": request.query_params.get("error")},
                  user=user, db=db, section="candidates")


@router.post("/merge")
def merge_do(keep: int = Form(...), drop: int = Form(...), user: User = Depends(require_staff),
             db: Session = Depends(get_db)) -> Response:
    try:
        merge_candidates(db, keep, drop, user)
    except MergeError as exc:
        db.rollback()
        return RedirectResponse(f"/merge?keep={keep}&drop={drop}&error={str(exc).replace(' ', '+')}", status_code=303)
    db.commit()
    return RedirectResponse(f"/candidates/{keep}?tab=activity&merged=1", status_code=303)


@router.post("/candidates/{candidate_id:int}/undo/{audit_id:int}")
def undo(candidate_id: int, audit_id: int, user: User = Depends(require_staff), db: Session = Depends(get_db)) -> Response:
    from app.core.models import AuditLog

    row = db.get(AuditLog, audit_id)
    try:
        if row is not None and row.action == "candidate.version_unlinked":
            undo_unlink(db, audit_id, user)
        else:
            undo_merge(db, audit_id, user)
    except MergeError as exc:
        db.rollback()
        return RedirectResponse(f"/candidates/{candidate_id}?tab=activity&error={str(exc).replace(' ', '+')}", status_code=303)
    db.commit()
    return RedirectResponse(f"/candidates/{candidate_id}?tab=activity&undone=1", status_code=303)


@router.post("/candidates/{candidate_id:int}/versions/{version_id:int}/unlink")
def unlink(candidate_id: int, version_id: int, user: User = Depends(require_staff), db: Session = Depends(get_db)) -> Response:
    version = db.get(CandidateVersion, version_id)
    if version is None or version.candidate_id != candidate_id:
        raise HTTPException(status_code=404)
    try:
        row = unlink_version(db, version_id, user)
    except MergeError as exc:
        db.rollback()
        return RedirectResponse(f"/candidates/{candidate_id}?tab=versions&error={str(exc).replace(' ', '+')}", status_code=303)
    db.commit()
    return RedirectResponse(f"/candidates/{(row.output or {})['snapshot']['new_candidate_id']}?unlinked=1", status_code=303)
