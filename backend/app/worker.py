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
from collections.abc import Callable
from pathlib import Path
from types import FrameType

from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.core import queue
from app.core.audit import audit
from app.core.db import SessionLocal
from app.workflows import cv_embed, cv_intake, hiring_request, retention, tickets  # noqa: F401 - importing registers the job handlers

log = logging.getLogger("worker")
_stop_event = threading.Event()


def _stop(signum: int, frame: FrameType | None) -> None:
    _stop_event.set()


def count_files(path: Path) -> int:
    """Counts files only. Never opens or reads them."""
    if not path.exists():
        return -1
    return sum(1 for p in path.iterdir() if p.is_file())


class IngestPacer:
    """Cool-down between batches of CVs. After every `batch_size` finished CV jobs, CV work pauses for
    `pause_seconds` (requests and other jobs keep running). batch_size 0 turns it off."""

    def __init__(self, batch_size: int, pause_seconds: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.batch_size, self.pause_seconds, self.clock = batch_size, pause_seconds, clock
        self._done = 0
        self._until = 0.0
        self._lock = threading.Lock()

    def paused(self) -> bool:
        with self._lock:
            return self.clock() < self._until

    def seconds_left(self) -> float:
        with self._lock:
            return max(0.0, self._until - self.clock())

    def finished_cv_job(self) -> None:
        if self.batch_size <= 0:
            return
        with self._lock:
            self._done += 1
            if self._done % self.batch_size == 0:
                self._until = self.clock() + self.pause_seconds
                log.info("Batch of %s CV jobs done: pausing CV work for %s s to let the laptop cool down",
                         self.batch_size, self.pause_seconds)


pacer = IngestPacer(settings.ingest_batch_size, settings.ingest_batch_pause_seconds)


def run_one(factory: sessionmaker[Session], worker_id: str, pacer: IngestPacer | None = None) -> bool:
    """Claim and run a single job. Returns False if there was nothing to do.

    A failing job never blocks others and never half-writes: the handler runs in its own
    transaction, which is rolled back on error before the failure is recorded.
    """
    with factory() as session:
        job = queue.claim(session, worker_id, exclude_prefix="cv." if pacer and pacer.paused() else None)
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
        if pacer and job_type == "cv.process":
            pacer.finished_cv_job()
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
            did_work = run_one(SessionLocal, worker_id, pacer)
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

    seen: dict[str, tuple[int, float]] = {}
    while not _stop_event.is_set():
        try:
            with SessionLocal() as session:
                queued = cv_intake.scan_folders(session, seen)
                retention.maybe_schedule(session)
        except Exception:  # noqa: BLE001 - e.g. database briefly unavailable; try again next poll
            log.exception("Folder scan failed")
            queued = 0
        log.info(
            "Heartbeat: cvs=%s files, inbox=%s files, newly queued=%s",
            count_files(settings.cvs_dir),
            count_files(settings.inbox_dir),
            queued,
        )
        _stop_event.wait(settings.poll_seconds)

    for t in threads:
        t.join(timeout=5)
    log.info("Worker stopped.")


if __name__ == "__main__":
    main()
