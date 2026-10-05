"""Postgres job queue. SELECT ... FOR UPDATE SKIP LOCKED lets many workers claim safely.

Lifecycle: queued -> running -> done, or back to queued with backoff on failure,
or dead after max_attempts (a review item is created so a human sees it).
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.core.audit import audit
from app.core.models import Job, ReviewItem

JobHandler = Callable[[Session, Job], None]
HANDLERS: dict[str, JobHandler] = {}


def register(job_type: str) -> Callable[[JobHandler], JobHandler]:
    """Decorator: register the function that runs jobs of this type."""

    def decorator(fn: JobHandler) -> JobHandler:
        HANDLERS[job_type] = fn
        return fn

    return decorator


def _now() -> datetime:
    return datetime.now(timezone.utc)


def enqueue(
    session: Session,
    job_type: str,
    payload: dict[str, Any] | None = None,
    *,
    dedupe_key: str | None = None,
    max_attempts: int | None = None,
    run_after: datetime | None = None,
) -> Job | None:
    """Add a job. With a dedupe_key, a second enqueue of the same key is ignored (returns None)."""
    values = {
        "type": job_type,
        "payload": payload or {},
        "status": "queued",
        "attempts": 0,
        "max_attempts": max_attempts or settings.job_max_attempts,
        "run_after": run_after or _now(),
        "dedupe_key": dedupe_key,
    }
    stmt = pg_insert(Job).values(**values)
    if dedupe_key is not None:
        stmt = stmt.on_conflict_do_nothing(index_elements=["dedupe_key"])
    job_id = session.execute(stmt.returning(Job.id)).scalar()
    session.flush()
    return session.get(Job, job_id) if job_id is not None else None


def claim(session: Session, worker_id: str, exclude_prefix: str | None = None) -> Job | None:
    """Claim one runnable job (or a stale running one from a crashed worker). Caller commits.

    exclude_prefix skips job types such as "cv." (used for the cool-down pause between CV batches).
    """
    now = _now()
    stale_before = now - timedelta(seconds=settings.job_stale_seconds)
    stmt = (
        select(Job)
        .where(
            or_(
                (Job.status == "queued") & (Job.run_after <= now),
                (Job.status == "running") & (Job.locked_at < stale_before),
            )
        )
        .order_by(Job.run_after, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if exclude_prefix:
        stmt = stmt.where(~Job.type.startswith(exclude_prefix))
    job = session.scalars(stmt).first()
    if job is None:
        return None
    job.status = "running"
    job.attempts += 1
    job.locked_at = now
    job.locked_by = worker_id
    session.flush()
    return job


def complete(session: Session, job: Job) -> None:
    job.status = "done"
    job.finished_at = _now()
    job.locked_at = None
    job.last_error = None
    session.flush()


def backoff_seconds(attempts: int) -> int:
    return min(5 * 2 ** (attempts - 1), 300)


def fail(session: Session, job: Job, error: str, *, permanent: bool = False) -> str:
    """Record a failure. Returns the new status: 'queued' (will retry) or 'dead'."""
    job.last_error = error[:2000]
    job.locked_at = None
    if permanent or job.attempts >= job.max_attempts:
        job.status = "dead"
        job.finished_at = _now()
        session.add(
            ReviewItem(
                type="job_failed",
                summary=f"Background task '{job.type}' failed and needs a person to look at it",
                reason=job.last_error,
                entity_type="job",
                entity_id=job.id,
                payload={"job_type": job.type, "attempts": job.attempts},
            )
        )
        audit(
            session,
            actor_type="system",
            actor_name="worker",
            action="job.dead",
            entity_type="job",
            entity_id=job.id,
            output={"type": job.type, "error": job.last_error},
            job_id=job.id,
        )
    else:
        job.status = "queued"
        job.run_after = _now() + timedelta(seconds=backoff_seconds(job.attempts))
    session.flush()
    return job.status
