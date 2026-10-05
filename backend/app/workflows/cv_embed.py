"""Workflow A, step 8: embeddings for semantic search and matching (nomic-embed-text, local).

One vector for the whole profile plus one per experience entry. Runs as its own job so a slow or
unavailable model never blocks profile creation.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core import queue
from app.core.audit import audit
from app.core.models import CandidateEmbedding, CandidateVersion, Job
from app.llm.ollama import get_client


def profile_text(data: dict[str, Any]) -> str:
    """Compact text for the profile vector. No contact details and no protected attributes."""
    parts = [str(data.get("name") or "")]
    title = next((e["title"] for e in data.get("experience") or [] if e.get("is_current")), None)
    if title:
        parts.append(f"Current role: {title}")
    if data.get("summary"):
        parts.append(str(data["summary"]))
    for e in (data.get("experience") or [])[:10]:
        parts.append(f"{e.get('title')} at {e.get('company')} ({e.get('industry') or 'unknown industry'})")
    for ed in (data.get("education") or [])[:5]:
        parts.append(" ".join(filter(None, [ed.get("degree"), ed.get("field"), ed.get("institution")])))
    if data.get("skills"):
        parts.append("Skills: " + ", ".join(data["skills"][:30]))
    if data.get("certifications"):
        parts.append("Certificates: " + ", ".join(data["certifications"]))
    return "\n".join(p for p in parts if p)


def experience_text(entry: dict[str, Any]) -> str:
    bits = [f"{entry.get('title')} at {entry.get('company')}"]
    if entry.get("industry"):
        bits.append(f"Industry: {entry['industry']}")
    if entry.get("description"):
        bits.append(str(entry["description"]))
    return "\n".join(bits)


def embed_version(session: Session, version: CandidateVersion, job_id: int | None = None) -> int:
    """(Re)build all embeddings for a version. Raises LLMUnavailable so the job retries later."""
    data = version.data or {}
    docs = [profile_text(data)]
    refs: list[tuple[str, str | None]] = [("profile", None)]
    for i, entry in enumerate((data.get("experience") or [])[:10]):
        docs.append(experience_text(entry))
        refs.append(("experience", str(i)))

    result = get_client().embed(docs, "document")
    session.execute(delete(CandidateEmbedding).where(CandidateEmbedding.candidate_version_id == version.id))
    for (kind, ref), vector in zip(refs, result.vectors, strict=True):
        session.add(
            CandidateEmbedding(
                candidate_version_id=version.id, kind=kind, ref=ref, embedding=vector, model=result.model
            )
        )
    audit(
        session,
        actor_type="agent",
        actor_name="embedder",
        action="agent.embed",
        entity_type="candidate_version",
        entity_id=version.id,
        input=docs,
        output={"vectors": len(docs)},
        model=result.model,
        latency_ms=result.latency_ms,
        job_id=job_id,
    )
    return len(docs)


@queue.register("cv.embed")
def handle_embed(session: Session, job: Job) -> None:
    version = session.get(CandidateVersion, job.payload["version_id"])
    if version is None:
        return
    embed_version(session, version, job.id)


def enqueue_embed(session: Session, version_id: int, tag: str = "initial") -> None:
    """tag keeps a re-embed after re-extraction from being swallowed by the earlier job's dedupe key."""
    queue.enqueue(session, "cv.embed", {"version_id": version_id}, dedupe_key=f"cv.embed:{version_id}:{tag}",
                  max_attempts=10)

