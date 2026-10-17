"""Run only the queued embedding jobs, now. Embeddings use the small embedding model, so this barely loads the GPU,
unlike CV extraction. Useful when a big ingest has left hundreds of embeddings waiting behind it.

    docker compose exec api python -m app.core.drain_embeds
"""
from __future__ import annotations

import logging
import socket
import time

from app.core import queue
from app.core.db import SessionLocal
import app.workflows.cv_embed  # noqa: F401  (registers the cv.embed handler)

log = logging.getLogger("drain_embeds")


def main() -> None:
    worker_id = f"drain-embeds@{socket.gethostname()}"
    done = failed = 0
    started = time.monotonic()
    while True:
        with SessionLocal() as s:
            job = queue.claim(s, worker_id, only_type="cv.embed")
            if job is None:
                s.commit()
                break
            try:
                queue.HANDLERS[job.type](s, job)
                queue.complete(s, job)
                done += 1
            except Exception as exc:  # noqa: BLE001 - one bad job must not stop the rest; it is retried by the worker
                s.rollback()
                with SessionLocal() as s2:
                    j = s2.get(type(job), job.id)
                    queue.fail(s2, j, str(exc)[:500])
                    s2.commit()
                failed += 1
                continue
            s.commit()
    print(f"embedding jobs done: {done}, failed (will be retried): {failed}, in {time.monotonic() - started:.0f}s")


if __name__ == "__main__":
    main()
