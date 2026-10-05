"""Public company e-form (no login). Opened through a signed, per-company link.

Everything submitted here is untrusted: lengths are capped, the attachment goes through the same safety
checks as CVs, the form is CSRF-protected and rate limited per IP, and a hidden honeypot field catches bots.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File as FormFile, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.db import get_db
from app.core.links import read_company_token
from app.core.models import File
from app.core.ratelimit import FailureLimiter
from app.core.uploads import save_upload
from app.core.web import render
from app.workflows.hiring_request import RequestInput, create_request, requests_dir

router = APIRouter()
submissions = FailureLimiter(3600)  # reuses the sliding window counter: one entry per submission
MAX_PER_IP_PER_HOUR = 10


def _company(token: str) -> str:
    company = read_company_token(token)
    if company is None:
        raise HTTPException(status_code=404)
    return company


@router.get("/apply/{token}")
def form(token: str, request: Request) -> Response:
    company = _company(token)
    return render(request, "public/form.html", {"company": company, "values": {}, "token": token})


@router.post("/apply/{token}")
async def submit(
    token: str,
    request: Request,
    contact_name: str = Form(""), contact_email: str = Form(""), job_title: str = Form(""),
    location: str = Form(""), num_roles: str = Form("1"), description: str = Form(""),
    must_text: str = Form(""), nice_text: str = Form(""), nationality: str = Form(""),
    nationality_justification: str = Form(""), start_date: str = Form(""), website: str = Form(""),
    attachment: UploadFile | None = FormFile(None),
    db: Session = Depends(get_db),
) -> Response:
    company = _company(token)
    ip = f"ip:{request.client.host if request.client else 'unknown'}"
    values = {"contact_name": contact_name, "contact_email": contact_email, "job_title": job_title,
              "location": location, "num_roles": num_roles, "description": description, "must_text": must_text,
              "nice_text": nice_text, "nationality": nationality,
              "nationality_justification": nationality_justification, "start_date": start_date}

    def again(message: str, status: int = 400) -> Response:
        return render(request, "public/form.html",
                      {"company": company, "values": values, "token": token, "error": message}, status_code=status)

    if submissions.blocked(ip, MAX_PER_IP_PER_HOUR):
        return again("Too many requests from this connection. Please try again later.", 429)
    if website.strip():  # honeypot: humans never see this field
        return RedirectResponse(f"/apply/{token}/done?ref=REQ-0000", status_code=303)

    errors = []
    if not contact_name.strip():
        errors.append("your name")
    if "@" not in contact_email or len(contact_email) > 320:
        errors.append("a valid email address")
    if not job_title.strip():
        errors.append("the job title")
    if not (description.strip() or must_text.strip() or (attachment is not None and attachment.filename)):
        errors.append("a description, requirements or a job description file")
    if nationality.strip() and len(nationality_justification.strip()) < 10:
        errors.append("the reason the role requires that nationality")
    if errors:
        return again("Please add " + ", ".join(errors) + ".")

    try:
        roles = int(num_roles or "1")
    except ValueError:
        roles = 1
    start = start_date.strip()
    try:
        if start:
            date.fromisoformat(start)
    except ValueError:
        return again("The start date is not a valid date.")

    file_row = None
    if attachment is not None and attachment.filename:
        saved = await save_upload(attachment, requests_dir())
        if isinstance(saved, str):
            return again(saved)
        path, sha = saved
        file_row = db.query(File).filter(File.sha256 == sha).first()
        if file_row is None:
            file_row = File(sha256=sha, original_name=path.name.split("_", 1)[-1], stored_path=str(path),
                            size_bytes=path.stat().st_size, origin="form", status="registered")
            db.add(file_row)
            db.flush()

    req = create_request(
        db,
        RequestInput(company=company, contact_name=contact_name[:300], contact_email=contact_email[:320],
                     job_title=job_title[:300], location=location[:300], num_roles=roles,
                     description=description[:20000], must_text=must_text[:5000], nice_text=nice_text[:5000],
                     nationality=nationality[:100], nationality_justification=nationality_justification[:1000],
                     start_date=start, via="form"),
        attachment_file=file_row,
    )
    audit(db, actor_type="system", actor_name=f"company-form:{company}", action="request.form_submitted",
          entity_type="request", entity_id=req.id, input=ip)
    db.commit()
    submissions.record(ip)
    return RedirectResponse(f"/apply/{token}/done?ref={req.reference}", status_code=303)


@router.get("/apply/{token}/done")
def done(token: str, request: Request, ref: str = "") -> Response:
    company = _company(token)
    return render(request, "public/done.html", {"company": company, "reference": ref[:30]})
