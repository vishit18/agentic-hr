"""Audit helper. Every agent step and human action goes through here.

Stores a SHA-256 of the input, never the raw input (CV text stays out of logs).
Writes inside the caller's transaction, so a rolled-back step leaves no audit row.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.orm import Session

from app.core.models import AuditLog


def hash_input(value: Any) -> str:
    """Stable SHA-256 of a string, bytes or JSON-able value."""
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode("utf-8")
    else:
        raw = json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def audit(
    session: Session,
    *,
    actor_type: str,
    action: str,
    actor_id: int | None = None,
    actor_name: str | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    input: Any = None,  # noqa: A002
    output: dict[str, Any] | None = None,
    model: str | None = None,
    prompt_version: str | None = None,
    latency_ms: int | None = None,
    job_id: int | None = None,
) -> AuditLog:
    row = AuditLog(
        actor_type=actor_type,
        actor_id=actor_id,
        actor_name=actor_name,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        input_hash=hash_input(input) if input is not None else None,
        output=output,
        model=model,
        prompt_version=prompt_version,
        latency_ms=latency_ms,
        job_id=job_id,
    )
    session.add(row)
    session.flush()
    return row
