"""Background worker entry point.

Runs WORKER_CONCURRENCY job threads (Postgres queue, SKIP LOCKED) plus a heartbeat
loop. Folder polling that triggers CV intake is added in Phase 1.
"""
from __future__ import annotations

import logging
import signal
import socket
import threading
import time
from pathlib import Path
from types import FrameType

from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.core import queue
from app.core.audit import audit
from app.core.db import SessionLocal

log = logging.getLogger("worker")
_stop_event = threading.Event()


def _stop(signum: int, frame: FrameType | None) -> None:
    _stop_event.set()


def count_files(path: Path) -> int:
    """Counts files only. Never opens or reads them."""
    if not path.exists():
        return -1
    return sum(1 for p in path.iterdir() if p.is_file())


def run_one(factory: "sessionmaker[Session]", worker_id: str) -> bool:
    """Claim and run a single job. Returns False if there was nothing to do.

    A failing job never blocks others and never half-writes: the handler runs in its own
    transaction, which is rolled back on error before the failure is recorded.
    """
    with factory() as session:
        job = queue.claim(session, worker_id)
        session.commit()
        if job is None:
            return False
        job_id, job_type = job.id, job.type

    started = time.monotonic()
    handler = queue.HANDLERS.get(job_type)
    try:
        if handler is None:
            raise LookupError(f"No handler registered for job type '{job_type}'")
        with factory() as session:
            running = session.get(queue.Job, job_id)
            assert running is not None
            handler(session, running)
            queue.complete(session, running)
            audit(
                session,
                actor_type="system",
                actor_name="worker",
                action="job.done",
                entity_type="job",
                entity_id=job_id,
                output={"type": job_type},
                latency_ms=int((time.monotonic() - started) * 1000),
                job_id=job_id,
            )
            session.commit()
    except Exception as exc:  # noqa: BLE001 - any handler error is recorded, never crashes the loop
        log.warning("Job %s (%s) failed: %s", job_id, job_type, exc.__class__.__name__)
        with factory() as session:
            failed = session.get(queue.Job, job_id)
            if failed is not None:
                queue.fail(session, failed, f"{exc.__class__.__name__}: {exc}", permanent=handler is None)
                session.commit()
    return True


def job_loop(worker_id: str) -> None:
    while not _stop_event.is_set():
        try:
            did_work = run_one(SessionLocal, worker_id)
        except Exception:  # noqa: BLE001 - e.g. database briefly unavailable
            log.exception("Worker loop error; retrying shortly")
            did_work = False
        if not did_work:
            _stop_event.wait(1.0)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    host = socket.gethostname()
    threads = [
        threading.Thread(target=job_loop, args=(f"{host}-{i}",), daemon=True, name=f"job-{i}")
        for i in range(settings.worker_concurrency)
    ]
    for t in threads:
        t.start()
    log.info(
        "Worker started: %s job threads, polling every %s seconds.",
        settings.worker_concurrency,
        settings.poll_seconds,
    )

    while not _stop_event.is_set():
        log.info(
            "Heartbeat: cvs=%s files, inbox=%s files",
            count_files(settings.cvs_dir),
            count_files(settings.inbox_dir),
        )
        _stop_event.wait(settings.poll_seconds)

    for t in threads:
        t.join(timeout=5)
    log.info("Worker stopped.")


if __name__ == "__main__":
    main()
