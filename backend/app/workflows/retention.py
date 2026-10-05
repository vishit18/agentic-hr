"""Retention (requirement 7.7): how long CVs are kept, and the scheduled purge.

Off by default (0 days). An administrator sets a number of days on the Settings page after seeing how many
profiles it would remove. A profile is due when its newest CV is older than the period. Profiles on a shortlist of
an open request are never purged. Originals in the read-only CV folder cannot be deleted by the hub; they are
reported so the source folder can be cleaned up in SharePoint.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import settings
from app.core import queue
from app.core.audit import audit
from app.core.models import (
    AppSetting,
    Candidate,
    CandidateEmbedding,
    CandidateIdentifier,
    CandidateVersion,
    File,
    Job,
    Note,
    Request,
    ShortlistItem,
)

KEY = "retention_days"
OPEN = ("new", "needs_info", "shortlisting", "ready_for_review")


def get_days(db: Session) -> int:
    row = db.get(AppSetting, KEY)
    try:
        return max(int(row.value), 0) if row else 0
    except ValueError:
        return 0


def set_days(db: Session, days: int, user_id: int | None) -> None:
    row = db.get(AppSetting, KEY)
    if row is None:
        db.add(AppSetting(key=KEY, value=str(days), updated_by=user_id))
    else:
        row.value, row.updated_by = str(days), user_id
    db.flush()


def due_candidates(db: Session, days: int, now: datetime | None = None) -> list[int]:
    if days <= 0:
        return []
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    protected = {cid for (cid,) in db.execute(
        select(ShortlistItem.candidate_id).join(Request, Request.id == ShortlistItem.request_id).where(Request.status.in_(OPEN)))}
    due = []
    for cid, applied, created in db.execute(
        select(Candidate.id, CandidateVersion.applied_at, CandidateVersion.created_at)
        .join(CandidateVersion, CandidateVersion.id == Candidate.current_version_id)
        .where(Candidate.merged_into_id.is_(None))):
        newest = applied or created
        if newest is not None and newest < cutoff and cid not in protected:
            due.append(cid)
    return due


def _writable(path: Path) -> bool:
    try:
        path.resolve().relative_to(settings.inbox_dir.resolve())
        return True
    except ValueError:
        return False


def purge_candidates(db: Session, ids: list[int], job_id: int | None = None) -> dict[str, int]:
    counts = {"profiles": 0, "versions": 0, "files_deleted": 0, "originals_left_in_source": 0}
    for cid in ids:
        cand = db.get(Candidate, cid)
        if cand is None:
            continue
        group = [cid, *[c for (c,) in db.execute(select(Candidate.id).where(Candidate.merged_into_id == cid))]]
        versions = list(db.scalars(select(CandidateVersion).where(CandidateVersion.candidate_id.in_(group))))
        vids = [v.id for v in versions]
        file_ids = {v.file_id for v in versions if v.file_id}
        for c in group:
            db.get(Candidate, c).current_version_id = None  # type: ignore[union-attr]
        db.flush()
        db.execute(delete(CandidateEmbedding).where(CandidateEmbedding.candidate_version_id.in_(vids or [-1])))
        db.execute(delete(CandidateIdentifier).where(CandidateIdentifier.candidate_id.in_(group)))
        db.execute(delete(ShortlistItem).where(ShortlistItem.candidate_id.in_(group)))
        db.execute(delete(Note).where(Note.entity_type == "candidate", Note.entity_id.in_(group)))
        db.execute(delete(CandidateVersion).where(CandidateVersion.id.in_(vids or [-1])))
        db.execute(delete(Candidate).where(Candidate.id.in_(group)))
        db.flush()
        for f in db.scalars(select(File).where(File.id.in_(file_ids or {-1}))):
            path = Path(f.stored_path)
            if _writable(path):
                path.unlink(missing_ok=True)
                counts["files_deleted"] += 1
            else:
                counts["originals_left_in_source"] += 1
            db.delete(f)
        counts["profiles"] += 1
        counts["versions"] += len(vids)
    audit(db, actor_type="system", actor_name="retention", action="retention.purge", output=counts, job_id=job_id)
    return counts


@queue.register("retention.purge")
def handle_purge(session: Session, job: Job) -> None:
    days = get_days(session)
    if days <= 0:
        return
    ids = due_candidates(session, days)
    if ids:
        purge_candidates(session, ids, job.id)


def maybe_schedule(session: Session) -> bool:
    """Called by the worker's poll loop: queue at most one purge per day, only when a period is set."""
    if get_days(session) <= 0:
        return False
    job = queue.enqueue(session, "retention.purge", {}, dedupe_key=f"retention:{date.today().isoformat()}")
    session.commit()
    return job is not None
