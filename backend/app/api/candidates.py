"""Candidates: search + filters, profile, CV upload, add sourced candidate, original file download."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File as FormFile, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.audit import audit
from app.core.auth import require_staff
from app.core.db import get_db
from app.core.uploads import save_upload as save_upload_to
from app.core.models import AuditLog, Candidate, CandidateVersion, File, User
from app.core.web import render
from app.matching.search import PAGE_SIZE, Filters, search, split_terms
from app.workflows.cv_intake import register_file
from app.workflows.manual_candidate import SOURCES, ManualInput, check_existing, create_manual

router = APIRouter()

TABS = [("overview", "Overview"), ("experience", "Experience"), ("education", "Education"),
        ("versions", "CV versions"), ("applications", "Applications"), ("activity", "Activity")]


def _float(value: str) -> float | None:
    try:
        return float(value) if value.strip() else None
    except ValueError:
        return None


def parse_filters(request: Request) -> Filters:
    p = request.query_params
    return Filters(
        q=p.get("q", "").strip()[:200],
        name=p.get("name", "").strip()[:100],
        skills=split_terms(p.get("skills", ""))[:10],
        certs=split_terms(p.get("certs", ""))[:10],
        min_years=_float(p.get("min_years", "")),
        max_years=_float(p.get("max_years", "")),
        education=p.get("education", "").strip()[:100],
        qualification=p.get("qualification", "").strip()[:100],
        industry=p.get("industry", "").strip()[:100],
        title=p.get("title", "").strip()[:100],
        nationality=p.get("nationality", "").strip()[:100],
        source=p.get("source", "").strip()[:100],
    )


def _sources(db: Session) -> list[str]:
    return [r[0] for r in db.execute(select(Candidate.source).where(Candidate.source.is_not(None)).distinct()).all()]


@router.get("/candidates")
def candidates_list(request: Request, page: int = 1, user: User = Depends(require_staff),
                    db: Session = Depends(get_db)) -> Response:
    filters = parse_filters(request)
    result = search(db, filters, page)
    nationality_available = db.scalar(select(CandidateVersion.id).where(CandidateVersion.nationality.is_not(None)).limit(1)) is not None
    ctx = {"f": filters, "result": result, "page_size": PAGE_SIZE, "uploaded": request.query_params.get("uploaded"),
           "nationality_available": nationality_available, "sources": _sources(db), "pages": max((result.total + PAGE_SIZE - 1) // PAGE_SIZE, 1)}
    template = "candidates/_results.html" if request.headers.get("hx-request") else "candidates/list.html"
    return render(request, template, ctx, user=user, db=db, section="candidates")


# ---------------------------------------------------------------- upload and add

async def save_upload(upload: UploadFile) -> tuple[Path, str] | str:
    """CV uploads land in the inbox folder, where the same workflow as any other CV picks them up."""
    return await save_upload_to(upload, settings.inbox_dir)


def queue_upload(db: Session, user: User, path: Path, sha: str, hints: dict | None) -> str:
    existing = db.scalar(select(File).where(File.sha256 == sha))
    if existing:
        path.unlink(missing_ok=True)
        return "That exact file is already in the database."
    payload = {"created_by": user.id}
    if hints:
        payload["hints"] = hints
    file = register_file(db, path, "upload", sha, payload)
    if file:
        audit(db, actor_type="user", actor_id=user.id, actor_name=user.email, action="file.uploaded",
              entity_type="file", entity_id=file.id, output={"name": file.original_name})
    db.commit()
    return ""


@router.post("/candidates/upload")
async def upload_cv(request: Request, cv: UploadFile = FormFile(...), user: User = Depends(require_staff),
                    db: Session = Depends(get_db)) -> Response:
    saved = await save_upload(cv)
    if isinstance(saved, str):
        return render(request, "candidates/list.html",
                      {"f": Filters(), "result": search(db, Filters()), "page_size": PAGE_SIZE, "upload_error": saved,
                       "sources": _sources(db), "pages": 1}, user=user, db=db, section="candidates", status_code=400)
    path, sha = saved
    error = queue_upload(db, user, path, sha, None)
    if error:
        return render(request, "candidates/list.html",
                      {"f": Filters(), "result": search(db, Filters()), "page_size": PAGE_SIZE, "upload_error": error,
                       "sources": _sources(db), "pages": 1}, user=user, db=db, section="candidates", status_code=409)
    return RedirectResponse("/candidates?uploaded=1", status_code=303)


def _form_ctx(**extra: object) -> dict:
    return {"sources": SOURCES, "values": {}, **extra}


@router.get("/candidates/new")
def add_form(request: Request, user: User = Depends(require_staff), db: Session = Depends(get_db)) -> Response:
    return render(request, "candidates/new.html", _form_ctx(), user=user, db=db, section="candidates")


@router.post("/candidates/new")
async def add_submit(
    request: Request,
    name: str = Form(""), email: str = Form(""), phone: str = Form(""), location: str = Form(""),
    source: str = Form("LinkedIn"), source_detail: str = Form(""), confirm: str = Form(""),
    cv: UploadFile | None = FormFile(None),
    user: User = Depends(require_staff), db: Session = Depends(get_db),
) -> Response:
    values = {"name": name, "email": email, "phone": phone, "location": location,
              "source": source, "source_detail": source_detail}

    def again(message: str, **extra: object) -> Response:
        return render(request, "candidates/new.html", _form_ctx(values=values, error=message, **extra),
                      user=user, db=db, section="candidates", status_code=400)

    if source not in SOURCES:
        return again("Choose where this candidate was found.")
    has_cv = cv is not None and bool(cv.filename)

    if has_cv:
        # CV path: same pipeline as any CV; the form details travel with the file as hints.
        if not (name.strip() or email.strip() or phone.strip()):
            return again("Add at least a name or a contact detail.")
        saved = await save_upload(cv)  # type: ignore[arg-type]
        if isinstance(saved, str):
            return again(saved)
        path, sha = saved
        hints = {"name": name, "emails": [email] if email.strip() else [], "phones": [phone] if phone.strip() else [],
                 "source": source, "source_detail": source_detail.strip() or None}
        error = queue_upload(db, user, path, sha, hints)
        if error:
            return again(error)
        return RedirectResponse("/candidates?uploaded=1", status_code=303)

    if not name.strip():
        return again("Please enter the candidate's name.")
    inp = ManualInput(name=name.strip(), email=email, phone=phone, source=source,
                      source_detail=source_detail, location=location)
    built, resolution = check_existing(db, inp)
    if resolution.action != "new" and confirm != "1":
        existing = db.get(Candidate, resolution.candidate_id) if resolution.candidate_id else None
        existing_version = db.get(CandidateVersion, existing.current_version_id) if existing else None
        return render(
            request, "candidates/new.html",
            _form_ctx(values=values, match=resolution, existing=existing, existing_version=existing_version),
            user=user, db=db, section="candidates",
        )
    candidate, _version, _res = create_manual(db, inp, user)
    db.commit()
    return RedirectResponse(f"/candidates/{candidate.id}", status_code=303)


# ---------------------------------------------------------------- profile

@router.get("/candidates/{candidate_id:int}")
def profile(candidate_id: int, request: Request, tab: str = "overview", user: User = Depends(require_staff),
            db: Session = Depends(get_db)) -> Response:
    candidate = db.get(Candidate, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404)
    if candidate.merged_into_id:
        return RedirectResponse(f"/candidates/{candidate.merged_into_id}", status_code=303)
    current = db.get(CandidateVersion, candidate.current_version_id) if candidate.current_version_id else None
    versions = db.scalars(
        select(CandidateVersion).where(CandidateVersion.candidate_id == candidate_id)
        .order_by(CandidateVersion.version_no.desc())
    ).all()
    files = {f.id: f for f in db.scalars(select(File).where(File.id.in_([v.file_id for v in versions if v.file_id] or [-1])))}
    if tab not in {t for t, _ in TABS}:
        tab = "overview"
    activity = []
    if tab == "activity":
        file_ids = list(files)
        version_ids = [v.id for v in versions]
        activity = db.scalars(
            select(AuditLog).where(or_(
                (AuditLog.entity_type == "candidate") & (AuditLog.entity_id == candidate_id),
                (AuditLog.entity_type == "file") & (AuditLog.entity_id.in_(file_ids or [-1])),
                (AuditLog.entity_type == "candidate_version") & (AuditLog.entity_id.in_(version_ids or [-1])),
            )).order_by(AuditLog.ts.desc()).limit(100)
        ).all()
    from app.workflows.merge import already_undone

    undoable = {a.id for a in activity if a.action in ("candidate.merged", "candidate.version_unlinked")
                and not already_undone(db, a.id)}
    qp = request.query_params
    return render(
        request, "candidates/profile.html",
        {"c": candidate, "v": current, "d": (current.data if current else {}) or {}, "versions": versions,
         "files": files, "tab": tab, "tabs": TABS, "activity": activity, "undoable": undoable,
         "flash": {"merged": qp.get("merged"), "undone": qp.get("undone"), "unlinked": qp.get("unlinked"),
                   "error": (qp.get("error") or "").replace("+", " ")}},
        user=user, db=db, section="candidates",
    )


@router.get("/files/{file_id:int}/original")
def original_file(file_id: int, user: User = Depends(require_staff), db: Session = Depends(get_db)) -> Response:
    """Originals are only served as attachments, never inline, and never from a raw path in the URL."""
    file = db.get(File, file_id)
    if file is None or not Path(file.stored_path).is_file():
        raise HTTPException(status_code=404)
    audit(db, actor_type="user", actor_id=user.id, actor_name=user.email, action="file.downloaded",
          entity_type="file", entity_id=file.id)
    db.commit()
    return FileResponse(
        file.stored_path, filename=file.original_name, media_type="application/octet-stream",
        headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"},
    )
