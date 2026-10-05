"""All database tables. Migrations live in backend/migrations."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import settings

ROLES = ("recruiter", "hr_admin", "engineer")


class Base(DeclarativeBase):
    pass


def _now() -> Any:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20))  # recruiter | hr_admin | engineer
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = _now()


class File(Base):
    """Every file the hub has seen (CV, request attachment). Unique by content hash."""

    __tablename__ = "files"

    id: Mapped[int] = mapped_column(primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    original_name: Mapped[str] = mapped_column(String(500))  # sanitised
    stored_path: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    mime_type: Mapped[str | None] = mapped_column(String(100))
    origin: Mapped[str] = mapped_column(String(30))  # cvs_dir | inbox | upload | form
    status: Mapped[str] = mapped_column(String(30), default="registered")
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class Candidate(Base):
    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(primary_key=True)
    # use_alter: candidates and candidate_versions point at each other
    current_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidate_versions.id", use_alter=True, name="fk_candidates_current_version")
    )
    source: Mapped[str | None] = mapped_column(String(100))  # cv_folder, linkedin, referral ...
    source_detail: Mapped[str | None] = mapped_column(Text)
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("candidates.id"))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CandidateVersion(Base):
    """One row per CV / application. History is kept; the candidate points at the current one."""

    __tablename__ = "candidate_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), index=True)
    file_id: Mapped[int | None] = mapped_column(ForeignKey("files.id"))
    version_no: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)  # extracted fields + snippets
    full_name: Mapped[str | None] = mapped_column(String(300), index=True)
    search_text: Mapped[str | None] = mapped_column(Text)
    search_tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', coalesce(search_text, ''))", persisted=True)
    )
    auto_linked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # pending | ok | ai_pending | failed
    extraction_status: Mapped[str] = mapped_column(String(30), default="pending")
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()

    __table_args__ = (Index("ix_candidate_versions_tsv", "search_tsv", postgresql_using="gin"),)


class CandidateEmbedding(Base):
    __tablename__ = "candidate_embeddings"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_version_id: Mapped[int] = mapped_column(ForeignKey("candidate_versions.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # profile | experience
    ref: Mapped[str | None] = mapped_column(String(100))  # experience index
    embedding: Mapped[Any] = mapped_column(Vector(settings.embedding_dim))
    model: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = _now()


class Request(Base):
    """A company hiring request."""

    __tablename__ = "requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    reference: Mapped[str] = mapped_column(String(30), unique=True)
    company: Mapped[str | None] = mapped_column(String(300), index=True)
    contact_name: Mapped[str | None] = mapped_column(String(300))
    contact_email: Mapped[str | None] = mapped_column(String(320))
    job_title: Mapped[str | None] = mapped_column(String(300))
    location: Mapped[str | None] = mapped_column(String(300))
    num_roles: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    description: Mapped[str | None] = mapped_column(Text)
    raw_text: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # new | needs_info | shortlisting | ready_for_review | shared | closed
    status: Mapped[str] = mapped_column(String(30), default="new", index=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    received_at: Mapped[datetime] = _now()
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RequestRequirement(Base):
    __tablename__ = "request_requirements"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # must | nice
    category: Mapped[str] = mapped_column(String(30))  # cert | years | title | industry | skill | nationality
    text: Mapped[str] = mapped_column(Text)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    source_snippet: Mapped[str | None] = mapped_column(Text)
    edited_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class ShortlistItem(Base):
    __tablename__ = "shortlist_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    agent_rank: Mapped[int | None] = mapped_column(Integer)  # what the agent suggested
    score: Mapped[float | None] = mapped_column(Float)
    score_breakdown: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    explanation: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # suggested | removed | approved | shared
    status: Mapped[str] = mapped_column(String(20), default="suggested")
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class ReviewItem(Base):
    """Anything waiting on a human: the "Needs you" inbox."""

    __tablename__ = "review_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    # shortlist_ready | possible_duplicate | low_confidence | file_unreadable
    # | request_needs_info | job_failed
    type: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)  # open | resolved
    summary: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    entity_type: Mapped[str | None] = mapped_column(String(30))
    entity_id: Mapped[int | None] = mapped_column(BigInteger)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _now()
    resolved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[str | None] = mapped_column(String(50))


class AuditLog(Base):
    """Every agent step and human action. Stores hashes and IDs, not raw CV text."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    actor_type: Mapped[str] = mapped_column(String(20))  # user | agent | system
    actor_id: Mapped[int | None] = mapped_column(Integer)
    actor_name: Mapped[str | None] = mapped_column(String(200))  # user email or agent name
    action: Mapped[str] = mapped_column(String(100), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(30))
    entity_id: Mapped[int | None] = mapped_column(BigInteger)
    input_hash: Mapped[str | None] = mapped_column(String(64))
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    model: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str | None] = mapped_column(String(50))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    job_id: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (Index("ix_audit_entity", "entity_type", "entity_id"),)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    type: Mapped[str] = mapped_column(String(60), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued | running | done | dead
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(100))
    last_error: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_jobs_claim", "status", "run_after"),)


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    reporter_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    message: Mapped[str] = mapped_column(Text)
    page_url: Mapped[str | None] = mapped_column(Text)
    record_refs: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    logs: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)  # attached audit rows / agent IO
    summary: Mapped[str | None] = mapped_column(Text)  # Ticket Triage agent
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | in_progress | fixed
    fix_note: Mapped[str | None] = mapped_column(Text)
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Note(Base):
    """Notes, comments and next steps on a request or candidate."""

    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(30))  # request | candidate | shortlist_item
    entity_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(20), default="note")  # note | next_step
    body: Mapped[str] = mapped_column(Text)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _now()

    __table_args__ = (Index("ix_notes_entity", "entity_type", "entity_id"),)


class Attachment(Base):
    """Files uploaded against a request (JD, supporting documents)."""

    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id", ondelete="CASCADE"), index=True)
    file_id: Mapped[int | None] = mapped_column(ForeignKey("files.id"))
    filename: Mapped[str] = mapped_column(String(500))
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _now()
