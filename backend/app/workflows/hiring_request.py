"""Workflow B: a hiring request arrives -> shortlist waiting for a recruiter.

Trigger: a company submits the tokenised e-form, or a recruiter pastes an email / uploads a JD.
Steps: validate -> parse (agent) -> completeness check -> assign owner -> filters -> retrieve -> score ->
       explain (agent) -> shortlist waiting. A person reviews, edits and approves before anything is shared.
"""
from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents import requirement_parser as rp
from app.config import settings
from app.core import queue
from app.core.audit import audit
from app.core.models import Attachment, File, Job, Request, RequestRequirement, ReviewItem, User
from app.llm.ollama import LLMUnavailable, LLMValidationError, get_client
from app.matching.scoring import tokens
from app.matching.shortlist import build_shortlist, requirements_of
from app.parsing.extract import ParseError, extract_text
from app.parsing.normalise import CERT_ALIASES
from app.parsing.safety import check_file

OPEN_STATUSES = ("new", "needs_info", "shortlisting", "ready_for_review")


@dataclass
class RequestInput:
    company: str
    contact_name: str = ""
    contact_email: str = ""
    job_title: str = ""
    location: str = ""
    num_roles: int = 1
    description: str = ""
    must_text: str = ""
    nice_text: str = ""
    nationality: str = ""
    nationality_justification: str = ""
    start_date: str = ""
    via: str = "form"  # form | recruiter


def requests_dir() -> Path:
    path = settings.inbox_dir / ".requests"  # dot-folder: the CV poller never looks inside
    path.mkdir(parents=True, exist_ok=True)
    return path


def create_request(session: Session, inp: RequestInput, *, attachment_file: File | None = None,
                   created_by: User | None = None) -> Request:
    req = Request(
        reference=f"tmp-{secrets.token_hex(6)}",
        company=inp.company.strip()[:300],
        contact_name=inp.contact_name.strip()[:300] or None,
        contact_email=inp.contact_email.strip().lower()[:320] or None,
        job_title=inp.job_title.strip()[:300] or None,
        location=inp.location.strip()[:300] or None,
        num_roles=max(1, min(inp.num_roles, 500)),
        description=inp.description.strip() or None,
        raw_text=inp.description.strip() or None,
        status="new",
        data={
            "via": inp.via, "must_text": inp.must_text.strip(), "nice_text": inp.nice_text.strip(),
            "start_date": inp.start_date.strip(), "nationality": inp.nationality.strip(),
            "nationality_justification": inp.nationality_justification.strip(),
        },
    )
    session.add(req)
    session.flush()
    req.reference = f"REQ-{datetime.now(timezone.utc).year}-{req.id:04d}"
    if attachment_file is not None:
        session.add(Attachment(request_id=req.id, file_id=attachment_file.id, filename=attachment_file.original_name,
                               uploaded_by=created_by.id if created_by else None))
    audit(session, actor_type="user" if created_by else "system", actor_id=created_by.id if created_by else None,
          actor_name=created_by.email if created_by else f"company-form:{req.company}",
          action="request.received", entity_type="request", entity_id=req.id,
          output={"reference": req.reference, "via": inp.via, "has_attachment": attachment_file is not None})
    queue.enqueue(session, "request.process", {"request_id": req.id}, dedupe_key=f"request.process:{req.id}")
    return req


# ---------------------------------------------------------------- pure helpers

def guess_category(text: str) -> str:
    low = text.lower()
    if any(re.search(rf"\b{re.escape(a)}\b", low) for aliases in CERT_ALIASES.values() for a in aliases):
        return "certification"
    if rp.YEARS_RE.search(text):
        return "years_experience"
    if re.search(r"\b(degree|bachelor|master|diploma|bsc|msc|mba)\b", low):
        return "education"
    return "other"


def covered(text: str, existing: list[str]) -> bool:
    want = set(tokens(text))
    if not want:
        return True
    return any(len(want & set(tokens(e))) / len(want) >= 0.6 for e in existing)


def missing_fields(req: Request, has_attachment: bool, n_requirements: int) -> list[str]:
    missing = []
    if not req.company:
        missing.append("the company name")
    if not req.job_title:
        missing.append("the job title")
    if not (req.description and len(req.description) >= 20) and not has_attachment and n_requirements == 0:
        missing.append("a description of the role or the requirements")
    return missing


def draft_question(req: Request, missing: list[str]) -> str:
    items = "\n".join(f"- {m}" for m in missing)
    return (
        f"Hello{' ' + req.contact_name if req.contact_name else ''},\n\n"
        f"Thank you for your hiring request{' for ' + req.job_title if req.job_title else ''}. "
        f"To start the search we still need:\n{items}\n\nCould you reply with these details?\n\nKind regards,\nCadence HR"
    )


def assign_owner(session: Session) -> User | None:
    """Fewest open requests wins; ties go to the lower user id. Reassignable by a person."""
    people = list(session.scalars(select(User).where(User.is_active, User.role.in_(("recruiter", "hr_admin")))
                                  .order_by(User.id)))
    if not people:
        return None
    load = dict(session.execute(
        select(Request.owner_id, func.count()).where(Request.status.in_(OPEN_STATUSES)).group_by(Request.owner_id)
    ).all())
    return min(people, key=lambda u: (load.get(u.id, 0), u.id))


# ---------------------------------------------------------------- steps

def source_text(session: Session, req: Request) -> tuple[str, int, list[str]]:
    """Everything the company told us, as one labelled block. Attachment text goes through the CV safety checks."""
    d = req.data or {}
    parts = [f"Job title: {req.job_title or ''}", f"Location: {req.location or ''}",
             f"Number of roles: {req.num_roles}", f"Start date: {d.get('start_date') or ''}"]
    if req.description:
        parts.append("Description:\n" + req.description)
    notes: list[str] = []
    n_attach = 0
    for att in session.scalars(select(Attachment).where(Attachment.request_id == req.id)):
        f = session.get(File, att.file_id) if att.file_id else None
        if f is None:
            continue
        n_attach += 1
        path = Path(f.stored_path)
        check = check_file(path) if path.exists() else None
        if check is None or not check.ok:
            notes.append(f"Attachment '{att.filename}' could not be read: {(check.reason if check else 'file missing')}")
            continue
        try:
            parts.append(f"Attachment ({att.filename}):\n" + extract_text(path, check).text)
        except ParseError as exc:
            notes.append(f"Attachment '{att.filename}' could not be read: {exc}")
    if d.get("must_text"):
        parts.append("Must-have (stated by the company):\n" + d["must_text"])
    if d.get("nice_text"):
        parts.append("Nice-to-have (stated by the company):\n" + d["nice_text"])
    return "\n\n".join(parts), n_attach, notes


def parse_requirements(session: Session, req: Request, text: str, job_id: int | None, *, require_ai: bool = False) -> bool:
    """Step 2. Returns True if the model was used. Stores requirements; falls back to the company's own lists."""
    d = req.data or {}
    started = time.monotonic()
    ai_used, parsed, model = True, None, None
    try:
        res = rp.parse_request(text, get_client())
        parsed, model = res.parsed, res.model
    except (LLMUnavailable, LLMValidationError) as exc:
        if require_ai:
            raise  # a retry job wants the model or nothing: let it be retried with backoff
        ai_used = False
        parsed = rp.fallback(d.get("must_text", ""), d.get("nice_text", ""))
        if isinstance(exc, LLMValidationError):
            d["parser_note"] = "model output invalid"

    must = [r for r in parsed.must_have if rp.grounded(r.text, text)]
    nice = [r for r in parsed.nice_to_have if rp.grounded(r.text, text)]
    # Nothing the company wrote is lost: its own list items missing from the parse are added
    for line in rp.split_lines(d.get("must_text", "")):
        if not covered(line, [r.text for r in must]):
            must.append(rp.Req(text=line, category=guess_category(line)))
    for line in rp.split_lines(d.get("nice_text", "")):
        if not covered(line, [r.text for r in must + nice]):
            nice.append(rp.Req(text=line, category=guess_category(line)))

    min_years = parsed.min_years_experience or rp.find_min_years(text)
    d["min_years"] = min_years
    if parsed.job_title and not req.job_title:
        req.job_title = parsed.job_title.strip()[:300]

    session.query(RequestRequirement).filter(
        RequestRequirement.request_id == req.id, RequestRequirement.edited_by.is_(None)
    ).delete()
    for kind, items in (("must", must), ("nice", nice)):
        for r in items:
            session.add(RequestRequirement(request_id=req.id, kind=kind, category=r.category, text=r.text.strip()[:500],
                                           value={}, source_snippet=None))
    if d.get("nationality"):
        session.add(RequestRequirement(
            request_id=req.id, kind="must", category="nationality", text=d["nationality"],
            value={"justification": d.get("nationality_justification")}, source_snippet="Stated by the company in the form"))
    d["ai_pending"] = not ai_used
    req.data = {**d}
    audit(session, actor_type="agent" if ai_used else "system", actor_name="requirement-parser" if ai_used else "parser-fallback",
          action="agent.parse_request", entity_type="request", entity_id=req.id, input=text,
          output={"must": len(must), "nice": len(nice), "min_years": min_years, "ai_used": ai_used},
          model=model, prompt_version=rp.PROMPT_VERSION if ai_used else None,
          latency_ms=int((time.monotonic() - started) * 1000), job_id=job_id)
    return ai_used


def finish_matching(session: Session, req: Request, job_id: int | None) -> dict[str, Any]:
    summary = build_shortlist(session, req, job_id)
    req.status = "ready_for_review"
    d = {**(req.data or {}), "match_summary": summary}
    req.data = d
    open_item = session.scalar(select(ReviewItem).where(
        ReviewItem.type == "shortlist_ready", ReviewItem.entity_id == req.id, ReviewItem.status == "open"))
    if open_item is None:
        session.add(ReviewItem(
            type="shortlist_ready", summary=f"Shortlist ready for {req.job_title or 'a role'} at {req.company}",
            reason=f"{summary['listed']} candidates ranked", entity_type="request", entity_id=req.id,
            owner_id=req.owner_id, payload={"reference": req.reference}))
    audit(session, actor_type="system", actor_name="matching", action="request.shortlist_ready",
          entity_type="request", entity_id=req.id, output=summary, job_id=job_id)
    return summary


@queue.register("request.process")
def process_request(session: Session, job: Job) -> None:
    req = session.get(Request, job.payload["request_id"])
    if req is None or req.status not in ("new", "needs_info"):
        return
    text, n_attach, notes = source_text(session, req)
    if len(text) > settings.llm_max_cv_chars:
        notes.append(f"This request is long: the AI read the first {settings.llm_max_cv_chars} of {len(text)} "
                     "characters, so check that no requirement is missing")
    parse_requirements(session, req, text, job.id)
    if notes:
        req.data = {**(req.data or {}), "attachment_notes": notes}

    n_reqs = len(requirements_of(session, req.id))
    missing = missing_fields(req, n_attach > 0, n_reqs)
    if missing:
        req.status = "needs_info"
        session.add(ReviewItem(
            type="request_needs_info", summary=f"Request from {req.company or 'a company'} is missing details",
            reason="Missing: " + ", ".join(missing), entity_type="request", entity_id=req.id,
            owner_id=req.owner_id, payload={"draft_question": draft_question(req, missing), "missing": missing}))
        audit(session, actor_type="system", actor_name="completeness", action="request.needs_info",
              entity_type="request", entity_id=req.id, output={"missing": missing}, job_id=job.id)
        return

    if req.owner_id is None:
        owner = assign_owner(session)
        if owner:
            req.owner_id = owner.id
            audit(session, actor_type="system", actor_name="assignment", action="request.owner_assigned",
                  entity_type="request", entity_id=req.id, output={"owner": owner.email, "rule": "fewest open requests"},
                  job_id=job.id)
    req.status = "shortlisting"
    session.flush()
    finish_matching(session, req, job.id)
    if (req.data or {}).get("ai_pending"):
        queue.enqueue(session, "request.reprocess", {"request_id": req.id}, dedupe_key=f"request.reprocess:{req.id}",
                      max_attempts=10, run_after=datetime.now(timezone.utc) + timedelta(seconds=60))


@queue.register("request.reprocess")
def reprocess_request(session: Session, job: Job) -> None:
    """The model was down on arrival. Parse again with the model and rebuild the shortlist."""
    req = session.get(Request, job.payload["request_id"])
    if req is None or not (req.data or {}).get("ai_pending"):
        return
    edited = session.scalar(select(func.count()).select_from(RequestRequirement).where(
        RequestRequirement.request_id == req.id, RequestRequirement.edited_by.is_not(None)))
    if edited or (req.data or {}).get("human_edited"):  # a person already corrected the requirements: do not overwrite their work
        req.data = {**(req.data or {}), "ai_pending": False}
        return
    text, _n, _notes = source_text(session, req)
    parse_requirements(session, req, text, job.id, require_ai=True)  # LLMUnavailable propagates: retried later
    finish_matching(session, req, job.id)


@queue.register("request.rematch")
def rematch_request(session: Session, job: Job) -> None:
    """A person edited the requirements: rebuild the shortlist (removed candidates stay removed)."""
    req = session.get(Request, job.payload["request_id"])
    if req is None:
        return
    finish_matching(session, req, job.id)
