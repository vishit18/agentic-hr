from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.core import queue
from app.core.models import AuditLog, Job, ReviewItem
from app.worker import run_one


@pytest.fixture(autouse=True)
def clean_handlers():
    saved = dict(queue.HANDLERS)
    queue.HANDLERS.clear()
    yield
    queue.HANDLERS.clear()
    queue.HANDLERS.update(saved)


def test_enqueue_dedupes(db) -> None:
    assert queue.enqueue(db, "t", {}, dedupe_key="same") is not None
    assert queue.enqueue(db, "t", {}, dedupe_key="same") is None
    db.commit()
    assert db.scalar(select(func.count()).select_from(Job)) == 1


def test_concurrent_workers_never_claim_the_same_job(db, session_factory) -> None:
    for i in range(30):
        queue.enqueue(db, "t", {"i": i})
    db.commit()

    def worker(name: str) -> list[int]:
        claimed: list[int] = []
        while True:
            with session_factory() as s:
                job = queue.claim(s, name)
                s.commit()
                if job is None:
                    return claimed
                claimed.append(job.id)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, [f"w{i}" for i in range(8)]))
    all_ids = [i for r in results for i in r]
    assert len(all_ids) == 30
    assert len(set(all_ids)) == 30


def test_successful_job_is_done_and_audited(db, session_factory) -> None:
    seen: list[int] = []

    @queue.register("ok")
    def handler(session, job) -> None:
        seen.append(job.id)

    queue.enqueue(db, "ok", {})
    db.commit()
    assert run_one(session_factory, "w") is True
    assert run_one(session_factory, "w") is False  # nothing left

    db.expire_all()
    job = db.scalars(select(Job)).one()
    assert job.status == "done" and seen == [job.id]
    assert db.scalar(select(AuditLog.action).where(AuditLog.action == "job.done")) == "job.done"


def test_failure_retries_with_backoff(db, session_factory) -> None:
    @queue.register("flaky")
    def handler(session, job) -> None:
        raise RuntimeError("boom")

    queue.enqueue(db, "flaky", {}, max_attempts=3)
    db.commit()
    run_one(session_factory, "w")

    db.expire_all()
    job = db.scalars(select(Job)).one()
    assert job.status == "queued" and job.attempts == 1
    assert job.run_after > datetime.now(timezone.utc) + timedelta(seconds=1)
    assert "boom" in (job.last_error or "")
    # Not runnable yet, so another claim finds nothing
    assert run_one(session_factory, "w") is False


def test_dead_letter_creates_review_item(db, session_factory) -> None:
    @queue.register("bad")
    def handler(session, job) -> None:
        raise ValueError("cannot read file")

    queue.enqueue(db, "bad", {}, max_attempts=1)
    db.commit()
    run_one(session_factory, "w")

    db.expire_all()
    job = db.scalars(select(Job)).one()
    assert job.status == "dead"
    item = db.scalars(select(ReviewItem)).one()
    assert item.type == "job_failed" and item.status == "open"
    assert "cannot read file" in (item.reason or "")
    assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "job.dead")) == 1


def test_failed_handler_leaves_no_partial_writes(db, session_factory) -> None:
    @queue.register("half")
    def handler(session, job) -> None:
        session.add(ReviewItem(type="low_confidence", summary="should not survive"))
        session.flush()
        raise RuntimeError("after write")

    queue.enqueue(db, "half", {}, max_attempts=3)
    db.commit()
    run_one(session_factory, "w")

    db.expire_all()
    assert db.scalar(select(func.count()).select_from(ReviewItem)) == 0


def test_unknown_job_type_goes_straight_to_dead(db, session_factory) -> None:
    queue.enqueue(db, "nobody-handles-this", {}, max_attempts=3)
    db.commit()
    run_one(session_factory, "w")
    db.expire_all()
    assert db.scalars(select(Job)).one().status == "dead"


def test_failing_job_does_not_block_others(db, session_factory) -> None:
    done: list[str] = []

    @queue.register("bad")
    def bad(session, job) -> None:
        raise RuntimeError("x")

    @queue.register("good")
    def good(session, job) -> None:
        done.append("good")

    queue.enqueue(db, "bad", {}, max_attempts=1)
    queue.enqueue(db, "good", {})
    db.commit()
    run_one(session_factory, "w")
    run_one(session_factory, "w")
    assert done == ["good"]


def test_stale_running_job_is_reclaimed(db, session_factory) -> None:
    job = queue.enqueue(db, "t", {})
    job.status = "running"
    job.locked_at = datetime.now(timezone.utc) - timedelta(hours=1)
    job.locked_by = "crashed-worker"
    db.commit()

    with session_factory() as s:
        claimed = queue.claim(s, "new-worker")
        s.commit()
        assert claimed is not None and claimed.locked_by == "new-worker"
