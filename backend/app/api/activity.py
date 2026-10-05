"""Activity: the audit timeline, and the live agent activity panel (HTMX polled every 3 seconds)."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request as HttpRequest
from fastapi.responses import Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.auth import require_staff
from app.core.db import get_db
from app.core.labels import action_label
from app.core.models import AuditLog, CandidateVersion, File, Job, Request, User
from app.core.web import ai_available, render

router = APIRouter()
PAGE = 50
JOB_LABELS = {
    "cv.process": "Reading a CV", "cv.embed": "Updating the search index", "cv.reextract": "Re-reading a CV with AI",
    "request.process": "Preparing a shortlist", "request.reprocess": "Upgrading a shortlist with AI",
    "request.rematch": "Updating a shortlist", "ticket.triage": "Summarising a problem report",
    "retention.purge": "Removing records past the retention period",
}
NOISE = ("job.done",)


def job_item(db: Session, job: Job) -> str:
    p = job.payload or {}
    if fid := p.get("file_id"):
        f = db.get(File, fid)
        return f.original_name if f else f"file {fid}"
    if rid := p.get("request_id"):
        r = db.get(Request, rid)
        return f"{r.reference} ({r.company})" if r else f"request {rid}"
    if vid := p.get("version_id"):
        v = db.get(CandidateVersion, vid)
        return v.full_name or f"profile {vid}" if v else f"profile {vid}"
    return ""


def job_rows(db: Session) -> list[dict]:
    now = datetime.now(timezone.utc)
    jobs = list(db.scalars(select(Job).where(Job.status.in_(("running", "queued", "dead")) | (Job.status == "done"))
                           .order_by(Job.id.desc()).limit(40)))
    rows = []
    for j in jobs:
        end = j.finished_at or now
        start = j.locked_at or j.finished_at or j.created_at
        seconds = max(0, int((end - start).total_seconds())) if j.status != "queued" else None
        rows.append({"id": j.id, "label": JOB_LABELS.get(j.type, j.type), "item": job_item(db, j), "status": j.status,
                     "seconds": seconds, "attempts": j.attempts, "waiting": j.status == "queued" and j.attempts > 0,
                     "error": j.last_error if j.status in ("queued", "dead") else None})
    order = {"running": 0, "queued": 1, "dead": 2, "done": 3}
    return sorted(rows, key=lambda r: (order[r["status"]], -r["id"]))


@router.get("/activity")
def activity(request: HttpRequest, tab: str = "timeline", candidate: str = "", req: str = "", user_q: str = "",
             agent: str = "", noise: str = "", page: int = 1, user: User = Depends(require_staff),
             db: Session = Depends(get_db)) -> Response:
    ctx: dict = {"tab": "agents" if tab == "agents" else "timeline"}
    if ctx["tab"] == "agents":
        ctx.update(jobs=job_rows(db), ai_ok=ai_available())
    else:
        stmt = select(AuditLog)
        if candidate.isdigit():
            vids = [v for (v,) in db.execute(select(CandidateVersion.id).where(CandidateVersion.candidate_id == int(candidate)))]
            fids = [f for (f,) in db.execute(select(CandidateVersion.file_id).where(CandidateVersion.candidate_id == int(candidate)))
                    if f]
            stmt = stmt.where(or_(
                (AuditLog.entity_type == "candidate") & (AuditLog.entity_id == int(candidate)),
                (AuditLog.entity_type == "candidate_version") & (AuditLog.entity_id.in_(vids or [-1])),
                (AuditLog.entity_type == "file") & (AuditLog.entity_id.in_(fids or [-1]))))
        if req.isdigit():
            stmt = stmt.where(AuditLog.entity_type == "request", AuditLog.entity_id == int(req))
        if user_q.strip():
            stmt = stmt.where(AuditLog.actor_type == "user", AuditLog.actor_name.ilike(f"%{user_q.strip()[:100]}%"))
        if agent.strip():
            stmt = stmt.where(AuditLog.actor_type.in_(("agent", "system")), AuditLog.actor_name.ilike(f"%{agent.strip()[:100]}%"))
        if not noise:
            stmt = stmt.where(AuditLog.action.notin_(NOISE), ~AuditLog.action.startswith("login."))
        total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = list(db.scalars(stmt.order_by(AuditLog.ts.desc()).limit(PAGE).offset((max(page, 1) - 1) * PAGE)))
        ctx.update(rows=rows, total=total, page=page, pages=max((total + PAGE - 1) // PAGE, 1),
                   q={"candidate": candidate, "req": req, "user_q": user_q, "agent": agent, "noise": noise})
    return render(request, "activity.html", ctx, user=user, db=db, section="activity")


@router.get("/activity/agents")
def agents_partial(request: HttpRequest, user: User = Depends(require_staff), db: Session = Depends(get_db)) -> Response:
    return render(request, "_agents.html", {"jobs": job_rows(db), "ai_ok": ai_available()}, user=user, db=db)


@router.get("/activity/jobs/{job_id:int}")
def job_detail(job_id: int, request: HttpRequest, user: User = Depends(require_staff), db: Session = Depends(get_db)) -> Response:
    """Plain-English account of one background task. Engineers get the raw detail through tickets."""
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404)
    steps = list(db.scalars(select(AuditLog).where(AuditLog.job_id == job_id).order_by(AuditLog.ts)))
    return render(request, "job_detail.html", {"job": job, "label": JOB_LABELS.get(job.type, job.type),
                                               "item": job_item(db, job), "steps": steps, "action_label": action_label},
                  user=user, db=db, section="activity")
