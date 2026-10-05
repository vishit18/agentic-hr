"""Problem reports: build the ticket with its logs, mask personal data, triage in the background."""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.agents import triage
from app.core import queue
from app.core.audit import audit
from app.core.models import AuditLog, CandidateVersion, Job, ReviewItem, Ticket, User
from app.llm.ollama import LLMError, get_client

EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"\+?\d[\d\s().\-]{7,}\d")
PER_ENTITY_LIMIT = 40


def mask_pii(value: Any) -> Any:
    """Engineers need to see what an agent did, not candidates' contact details."""
    if isinstance(value, str):
        return PHONE.sub("[phone]", EMAIL.sub("[email]", value))
    if isinstance(value, list):
        return [mask_pii(v) for v in value]
    if isinstance(value, dict):
        return {k: mask_pii(v) for k, v in value.items()}
    return value


def record_refs(page_url: str | None) -> dict[str, int]:
    """Which records the person was looking at, taken from the page address."""
    refs: dict[str, int] = {}
    for key, pattern in (("candidate_id", r"/candidates/(\d+)"), ("request_id", r"/requests/(\d+)"),
                         ("review_item_id", r"/needs-you/(\d+)")):
        m = re.search(pattern, page_url or "")
        if m:
            refs[key] = int(m.group(1))
    return refs


def _entities(db: Session, refs: dict[str, int]) -> list[tuple[str, int]]:
    ents: list[tuple[str, int]] = []
    if cid := refs.get("candidate_id"):
        ents.append(("candidate", cid))
        versions = list(db.scalars(select(CandidateVersion).where(CandidateVersion.candidate_id == cid)))
        ents += [("candidate_version", v.id) for v in versions]
        ents += [("file", v.file_id) for v in versions if v.file_id]
    if rid := refs.get("request_id"):
        ents.append(("request", rid))
    if iid := refs.get("review_item_id"):
        item = db.get(ReviewItem, iid)
        ents.append(("review_item", iid))
        if item and item.entity_type and item.entity_id:
            ents.append((item.entity_type, item.entity_id))
    return ents


def collect_logs(db: Session, refs: dict[str, int]) -> dict[str, Any]:
    rows: dict[int, AuditLog] = {}
    for etype, eid in _entities(db, refs):
        for r in db.scalars(select(AuditLog).where(AuditLog.entity_type == etype, AuditLog.entity_id == eid)
                            .order_by(AuditLog.ts.desc()).limit(PER_ENTITY_LIMIT)):
            rows[r.id] = r
    audit_rows = [
        {"id": r.id, "ts": r.ts.isoformat(), "actor": r.actor_name or r.actor_type, "action": r.action,
         "entity_type": r.entity_type, "entity_id": r.entity_id, "model": r.model, "prompt_version": r.prompt_version,
         "latency_ms": r.latency_ms, "input_hash": r.input_hash, "job_id": r.job_id, "output": mask_pii(r.output)}
        for r in sorted(rows.values(), key=lambda r: r.ts, reverse=True)
    ]
    job_ids = {r["job_id"] for r in audit_rows if r["job_id"]}
    jobs = [{"id": j.id, "type": j.type, "status": j.status, "attempts": j.attempts, "last_error": mask_pii(j.last_error)}
            for j in db.scalars(select(Job).where(or_(Job.id.in_(job_ids or [-1]), Job.status == "dead")).order_by(Job.id.desc()).limit(15))]
    return {"audit": audit_rows, "jobs": jobs}


def create_ticket(db: Session, user: User, message: str, page_url: str | None) -> Ticket:
    refs = record_refs(page_url)
    ticket = Ticket(reporter_id=user.id, message=message.strip()[:2000], page_url=(page_url or "")[:500] or None,
                    record_refs=refs, logs=collect_logs(db, refs), status="open")
    db.add(ticket)
    db.flush()
    audit(db, actor_type="user", actor_id=user.id, actor_name=user.email, action="ticket.created",
          entity_type="ticket", entity_id=ticket.id, output={"page": ticket.page_url, "refs": refs,
                                                             "log_rows": len(ticket.logs["audit"])})
    queue.enqueue(db, "ticket.triage", {"ticket_id": ticket.id}, dedupe_key=f"ticket.triage:{ticket.id}")
    return ticket


@queue.register("ticket.triage")
def triage_ticket(session: Session, job: Job) -> None:
    ticket = session.get(Ticket, job.payload["ticket_id"])
    if ticket is None or ticket.summary:
        return
    try:
        res = triage.summarise(ticket.message, get_client())
        ticket.summary = res.parsed.summary.strip()[:300]
        audit(session, actor_type="agent", actor_name="triage", action="agent.triage", entity_type="ticket",
              entity_id=ticket.id, input=ticket.message, output={"summary": ticket.summary}, model=res.model,
              prompt_version=triage.PROMPT_VERSION, latency_ms=res.latency_ms, job_id=job.id)
    except LLMError:
        ticket.summary = triage.fallback(ticket.message)
        audit(session, actor_type="system", actor_name="triage-fallback", action="agent.triage", entity_type="ticket",
              entity_id=ticket.id, output={"fallback": "first words"}, job_id=job.id)


def agent_input_text(db: Session, row: AuditLog) -> str | None:
    """The raw text an agent step worked on, fetched from where the hub keeps it. None if it was not stored."""
    if row.action == "agent.extract" and row.entity_type == "file" and row.entity_id:
        v = db.scalar(select(CandidateVersion).where(CandidateVersion.file_id == row.entity_id))
        return v.search_text if v else None
    if row.action == "agent.extract" and row.entity_type == "candidate_version" and row.entity_id:
        v = db.get(CandidateVersion, row.entity_id)
        return v.search_text if v else None
    if row.action == "agent.parse_request" and row.entity_type == "request" and row.entity_id:
        from app.core.models import Request
        r = db.get(Request, row.entity_id)
        return "\n\n".join(x for x in [r.description, (r.data or {}).get("must_text"), (r.data or {}).get("nice_text")] if x) if r else None
    return None
