"""Export everything Agentic HR holds (requirement 7.2): readable CSV and JSON plus the original files, in one zip."""
from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

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
    Ticket,
    User,
)

README = """Agentic HR Hub: full data export
Created: {when} by {who}

candidates.csv / candidates.json   one row per person, with every CV version in the JSON
requests.csv / requests.json       hiring requests with requirements, shortlist, notes and attachments
review_items.csv, tickets.csv      things that were waiting on people, and reported problems
audit_log.csv                      every recorded action
files/                             the original CV and attachment files, named <file id>_<original name>

Passwords and security secrets are never exported.
"""


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


def _csv(rows: list[dict[str, Any]], fields: list[str]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: (json.dumps(v, default=str) if isinstance(v, (list, dict)) else _iso(v)) for k, v in r.items()})
    return buf.getvalue()


def _json(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=_iso, ensure_ascii=False)


def build_export(db: Session, dest: Path, who: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    files = {f.id: f for f in db.scalars(select(File))}
    cands = []
    for c in db.scalars(select(Candidate).order_by(Candidate.id)):
        versions = list(db.scalars(select(CandidateVersion).where(CandidateVersion.candidate_id == c.id).order_by(CandidateVersion.version_no)))
        cur = next((v for v in versions if v.id == c.current_version_id), None)
        d = (cur.data if cur else {}) or {}
        cands.append({
            "id": c.id, "name": cur.full_name if cur else None, "current_title": cur.current_title if cur else None,
            "years_experience": cur.years_experience if cur else None, "emails": d.get("emails", []),
            "phones": d.get("phones", []), "certifications": cur.certifications if cur else [],
            "skills": cur.skills if cur else [], "source": c.source, "source_detail": c.source_detail,
            "merged_into_id": c.merged_into_id, "created_at": c.created_at,
            "versions": [{"version_no": v.version_no, "applied_at": v.applied_at, "auto_linked": v.auto_linked,
                          "extraction_status": v.extraction_status, "is_current": v.id == c.current_version_id,
                          "file": f"files/{v.file_id}_{files[v.file_id].original_name}" if v.file_id in files else None,
                          "profile": v.data} for v in versions],
        })
    counts["candidates"] = len(cands)

    reqs = []
    users = {u.id: u.email for u in db.scalars(select(User))}
    for r in db.scalars(select(Request).order_by(Request.id)):
        reqs.append({
            "id": r.id, "reference": r.reference, "company": r.company, "contact_name": r.contact_name,
            "contact_email": r.contact_email, "job_title": r.job_title, "location": r.location, "num_roles": r.num_roles,
            "description": r.description, "status": r.status, "owner": users.get(r.owner_id), "received_at": r.received_at,
            "requirements": [{"kind": q.kind, "category": q.category, "text": q.text} for q in db.scalars(
                select(RequestRequirement).where(RequestRequirement.request_id == r.id))],
            "shortlist": [{"candidate_id": i.candidate_id, "rank": i.rank, "agent_rank": i.agent_rank, "score": i.score,
                           "status": i.status, "decided_by": users.get(i.decided_by), "decided_at": i.decided_at,
                           "explanation": i.explanation} for i in db.scalars(
                select(ShortlistItem).where(ShortlistItem.request_id == r.id).order_by(ShortlistItem.rank))],
            "notes": [{"kind": n.kind, "body": n.body, "author": users.get(n.author_id), "created_at": n.created_at}
                      for n in db.scalars(select(Note).where(Note.entity_type == "request", Note.entity_id == r.id))],
            "attachments": [f"files/{a.file_id}_{a.filename}" for a in db.scalars(select(Attachment).where(Attachment.request_id == r.id))],
        })
    counts["requests"] = len(reqs)

    review = [{"id": i.id, "type": i.type, "status": i.status, "summary": i.summary, "reason": i.reason,
               "resolution": i.resolution, "created_at": i.created_at} for i in db.scalars(select(ReviewItem))]
    tickets = [{"id": t.id, "reporter": users.get(t.reporter_id), "message": t.message, "status": t.status,
                "fix_note": t.fix_note, "page_url": t.page_url, "created_at": t.created_at} for t in db.scalars(select(Ticket))]
    audit_rows = [{"id": a.id, "ts": a.ts, "actor": a.actor_name or a.actor_type, "action": a.action,
                   "entity_type": a.entity_type, "entity_id": a.entity_id, "model": a.model,
                   "prompt_version": a.prompt_version, "latency_ms": a.latency_ms, "input_hash": a.input_hash,
                   "output": a.output} for a in db.scalars(select(AuditLog).order_by(AuditLog.id))]
    counts.update(review_items=len(review), tickets=len(tickets), audit_rows=len(audit_rows))

    copied = 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", README.format(when=datetime.now(timezone.utc).isoformat(timespec="seconds"), who=who))
        z.writestr("candidates.json", _json(cands))
        z.writestr("candidates.csv", _csv(cands, ["id", "name", "current_title", "years_experience", "emails", "phones",
                                                  "certifications", "skills", "source", "source_detail", "merged_into_id", "created_at"]))
        z.writestr("requests.json", _json(reqs))
        z.writestr("requests.csv", _csv(reqs, ["id", "reference", "company", "contact_name", "contact_email", "job_title",
                                               "location", "num_roles", "status", "owner", "received_at", "description"]))
        z.writestr("review_items.csv", _csv(review, ["id", "type", "status", "summary", "reason", "resolution", "created_at"]))
        z.writestr("tickets.csv", _csv(tickets, ["id", "reporter", "message", "status", "fix_note", "page_url", "created_at"]))
        z.writestr("audit_log.csv", _csv(audit_rows, ["id", "ts", "actor", "action", "entity_type", "entity_id", "model",
                                                      "prompt_version", "latency_ms", "input_hash", "output"]))
        for fid, f in files.items():
            path = Path(f.stored_path)
            if path.is_file():
                z.write(path, f"files/{fid}_{f.original_name}")
                copied += 1
    counts["files"] = copied
    return counts
