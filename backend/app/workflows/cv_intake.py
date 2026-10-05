"""Workflow A: a CV arrives -> profile (or review item) waiting for HR.

Trigger: the worker polls CVS_DIR and INBOX_DIR (UI uploads land in INBOX_DIR).
Steps: register + hash -> safety -> text (OCR fallback) -> extract (agent) -> validate/normalise
       -> identity resolution -> write version -> embed (separate job) -> review items if needed.
Every step writes an audit row. A step that cannot finish routes to the review queue with a reason.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents import adjudicator, extractor
from app.config import settings
from app.core import queue
from app.core.audit import audit
from app.core.models import (
    Candidate,
    CandidateIdentifier,
    CandidateVersion,
    File,
    Job,
    ReviewItem,
)
from app.dedup.identity import Resolution, resolve
from app.llm.ollama import LLMUnavailable, LLMValidationError, get_client
from app.parsing.extract import OcrUnavailable, ParseError, extract_text
from app.parsing.profile import BuiltProfile, build_profile
from app.parsing.safety import check_file, sanitize_filename
from app.workflows.cv_embed import enqueue_embed

log = logging.getLogger("cv_intake")

ORIGIN_SOURCE = {"cvs_dir": "cv_folder", "inbox": "inbox", "upload": "upload"}
SETTLE_SECONDS = 2  # ignore files still being written


# ---------------------------------------------------------------- trigger: folder polling

def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def register_file(session: Session, path: Path, origin: str, sha: str | None = None) -> File | None:
    """Step 1: hash the file. A hash we have seen before is an exact duplicate: log and stop."""
    sha = sha or sha256_of(path)
    existing = session.scalar(select(File).where(File.sha256 == sha))
    if existing:
        audit(
            session,
            actor_type="system",
            actor_name="intake",
            action="file.exact_duplicate",
            entity_type="file",
            entity_id=existing.id,
            input=sha,
            output={"name": sanitize_filename(path.name), "origin": origin},
        )
        return None
    file = File(
        sha256=sha,
        original_name=sanitize_filename(path.name),
        stored_path=str(path),
        size_bytes=path.stat().st_size,
        origin=origin,
        status="registered",
    )
    session.add(file)
    session.flush()
    audit(
        session,
        actor_type="system",
        actor_name="intake",
        action="file.registered",
        entity_type="file",
        entity_id=file.id,
        input=sha,
        output={"name": file.original_name, "origin": origin, "bytes": file.size_bytes},
    )
    queue.enqueue(session, "cv.process", {"file_id": file.id}, dedupe_key=f"cv.process:{file.id}")
    return file


def _iter_files(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    out = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        out.extend(Path(root) / f for f in files if not f.startswith(".") and not f.endswith((".part", ".tmp")))
    return sorted(out)


def scan_folders(session: Session, seen: dict[str, tuple[int, float]]) -> int:
    """Register new or changed files in CVS_DIR and INBOX_DIR. Returns how many new files were queued."""
    queued = 0
    limit = settings.intake_max_files
    total = session.scalar(select(func.count()).select_from(File)) or 0
    for origin, folder in (("cvs_dir", settings.cvs_dir), ("inbox", settings.inbox_dir)):
        for path in _iter_files(folder):
            if limit and total >= limit:
                return queued
            try:
                st = path.stat()
            except OSError:
                continue
            if time.time() - st.st_mtime < SETTLE_SECONDS:
                continue
            key = str(path)
            if seen.get(key) == (st.st_size, st.st_mtime):
                continue
            try:
                registered = register_file(session, path, origin)
                session.commit()
            except Exception:  # noqa: BLE001 - one bad file must not stop the scan
                session.rollback()
                log.exception("Could not register %s", sanitize_filename(path.name))
                continue
            seen[key] = (st.st_size, st.st_mtime)
            if registered:
                queued += 1
                total += 1
    return queued


# ---------------------------------------------------------------- the job

def _review(session: Session, type_: str, summary: str, reason: str | None, entity_type: str, entity_id: int,
            payload: dict[str, Any] | None = None) -> ReviewItem:
    item = ReviewItem(type=type_, summary=summary, reason=reason, entity_type=entity_type,
                      entity_id=entity_id, payload=payload or {})
    session.add(item)
    session.flush()
    return item


def _reject(session: Session, file: File, status: str, reason: str, job_id: int) -> None:
    file.status = status
    file.reason = reason
    _review(session, "file_unreadable", f"'{file.original_name}' could not be read", reason, "file", file.id)
    audit(session, actor_type="system", actor_name="intake", action=f"file.{status}",
          entity_type="file", entity_id=file.id, output={"reason": reason}, job_id=job_id)


def _file_time(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def apply_profile(version: CandidateVersion, built: BuiltProfile) -> None:
    version.data = built.data
    for key, value in built.columns.items():
        setattr(version, key, value)


def add_identifiers(session: Session, candidate_id: int, version_id: int, data: dict[str, Any]) -> None:
    have = {
        (i.kind, i.value)
        for i in session.scalars(select(CandidateIdentifier).where(CandidateIdentifier.candidate_id == candidate_id))
    }
    for kind, values in (("email", data.get("emails") or []), ("phone", data.get("phones") or [])):
        for value in values:
            if (kind, value) not in have:
                session.add(CandidateIdentifier(candidate_id=candidate_id, candidate_version_id=version_id,
                                                kind=kind, value=value))
                have.add((kind, value))


def run_extraction(session: Session, text: str, file_id: int | None, job_id: int | None) -> tuple[BuiltProfile, str, str | None]:
    """Steps 5 and 6. Returns (profile, extraction_status, model). Falls back to code when the model fails."""
    client = get_client()
    started = time.monotonic()
    status, model, extracted, ai_used, note = "ok", None, None, True, None
    try:
        result = extractor.extract(text, client)
        extracted, model = result.parsed, result.model
    except LLMUnavailable:
        extracted, ai_used, status, note = extractor.fallback(text), False, "ai_pending", "model unavailable"
    except LLMValidationError:
        extracted, ai_used, status, note = extractor.fallback(text), False, "failed", "model output invalid"
    built = build_profile(extracted, text, ai_used=ai_used)
    if note and status == "failed":
        built.issues.append("The AI output could not be validated; basic details were read by code only")
        built.low_confidence = True
    audit(
        session,
        actor_type="agent" if ai_used else "system",
        actor_name="extractor" if ai_used else "extractor-fallback",
        action="agent.extract",
        entity_type="file",
        entity_id=file_id,
        input=text,
        output={"status": status, "note": note, "experience": len(built.data["experience"]),
                "education": len(built.data["education"]), "skills": len(built.data["skills"]),
                "issues": built.issues[:5]},
        model=model,
        prompt_version=extractor.PROMPT_VERSION if ai_used else None,
        latency_ms=int((time.monotonic() - started) * 1000),
        job_id=job_id,
    )
    return built, status, model


def maybe_adjudicate(session: Session, incoming: dict[str, Any], existing_version: CandidateVersion | None,
                     job_id: int | None) -> dict[str, Any] | None:
    """Step 7b: the Adjudicator suggests same/different for a possible match. Suggestion only."""
    if existing_version is None:
        return None
    started = time.monotonic()
    try:
        result = adjudicator.adjudicate(incoming, existing_version.data or {}, get_client())
    except (LLMUnavailable, LLMValidationError):
        return None
    suggestion = {"verdict": result.parsed.verdict, "reasons": result.parsed.reasons,
                  "model": result.model, "prompt_version": adjudicator.PROMPT_VERSION}
    audit(session, actor_type="agent", actor_name="adjudicator", action="agent.adjudicate",
          entity_type="candidate", entity_id=existing_version.candidate_id, input=[incoming, existing_version.data],
          output={"verdict": suggestion["verdict"]}, model=result.model, prompt_version=adjudicator.PROMPT_VERSION,
          latency_ms=int((time.monotonic() - started) * 1000), job_id=job_id)
    return suggestion


def write_version(session: Session, resolution: Resolution, built: BuiltProfile, status: str, file: File | None,
                  *, source: str, applied_at: datetime, created_by: int | None = None) -> tuple[Candidate, CandidateVersion]:
    """Step 9: new version row; the candidate's current pointer moves to the latest application."""
    if resolution.action == "auto_link" and resolution.candidate_id:
        candidate = session.get(Candidate, resolution.candidate_id)
        assert candidate is not None
    else:
        candidate = Candidate(source=source, created_by=created_by)
        session.add(candidate)
        session.flush()

    version_no = (session.scalar(select(func.max(CandidateVersion.version_no))
                                 .where(CandidateVersion.candidate_id == candidate.id)) or 0) + 1
    version = CandidateVersion(
        candidate_id=candidate.id,
        file_id=file.id if file else None,
        version_no=version_no,
        auto_linked=resolution.action == "auto_link",
        extraction_status=status,
        applied_at=applied_at,
    )
    apply_profile(version, built)
    session.add(version)
    session.flush()

    current = session.get(CandidateVersion, candidate.current_version_id) if candidate.current_version_id else None
    if current is None or (current.applied_at or applied_at) <= applied_at:
        candidate.current_version_id = version.id
    add_identifiers(session, candidate.id, version.id, built.data)
    session.flush()
    return candidate, version


def finish_candidate(session: Session, resolution: Resolution, candidate: Candidate, version: CandidateVersion,
                     built: BuiltProfile, label: str, job_id: int | None) -> None:
    """Step 10: review items for anything a person must look at."""
    if resolution.action in {"possible_match", "ambiguous"}:
        others = [resolution.candidate_id, *resolution.other_candidate_ids]
        suggestion = None
        if resolution.action == "possible_match" and resolution.candidate_id:
            existing = session.get(Candidate, resolution.candidate_id)
            existing_version = session.get(CandidateVersion, existing.current_version_id) if existing else None
            suggestion = maybe_adjudicate(session, built.data, existing_version, job_id)
        _review(
            session,
            "possible_duplicate",
            f"{built.data.get('name') or label} may already be in the database",
            "; ".join(resolution.evidence),
            "candidate",
            candidate.id,
            {"incoming_candidate_id": candidate.id, "existing_candidate_ids": others,
             "evidence": resolution.evidence, "score": resolution.score, "kind": resolution.action,
             "suggestion": suggestion},
        )
    if built.low_confidence:
        _review(session, "low_confidence", f"Check the details read from {label}",
                "; ".join(built.issues) or "Low confidence extraction", "candidate", candidate.id)


@queue.register("cv.process")
def process_cv(session: Session, job: Job) -> None:
    file = session.get(File, job.payload["file_id"])
    if file is None or session.scalar(select(CandidateVersion.id).where(CandidateVersion.file_id == file.id)):
        return  # idempotent: already processed
    path = Path(file.stored_path)
    started = time.monotonic()

    # step 2: type detection + safety
    if not path.exists():
        return _reject(session, file, "missing", "The file is no longer in its folder.", job.id)
    check = check_file(path)
    audit(session, actor_type="system", actor_name="safety", action="file.safety", entity_type="file",
          entity_id=file.id, output={"ok": check.ok, "kind": check.kind, "mime": check.mime, "reason": check.reason},
          job_id=job.id)
    if not check.ok:
        return _reject(session, file, "rejected", check.reason or "File failed safety checks.", job.id)
    file.mime_type = check.mime

    # steps 3-4: text, with OCR fallback
    try:
        text_result = extract_text(path, check)
    except OcrUnavailable:
        raise  # infrastructure problem: let the job retry
    except ParseError as exc:
        return _reject(session, file, "unreadable", str(exc), job.id)
    audit(session, actor_type="system", actor_name="parser", action="file.text", entity_type="file",
          entity_id=file.id, output={"method": text_result.method, "chars": len(text_result.text),
                                     "pages": text_result.pages}, job_id=job.id)

    # steps 5-6
    built, status, _model = run_extraction(session, text_result.text, file.id, job.id)

    # step 7
    resolution = resolve(session, built.data)
    audit(session, actor_type="system", actor_name="identity", action="identity.resolved", entity_type="file",
          entity_id=file.id, output={"action": resolution.action, "candidate_id": resolution.candidate_id,
                                     "evidence": resolution.evidence}, job_id=job.id)

    # step 9
    candidate, version = write_version(
        session, resolution, built, status, file,
        source=ORIGIN_SOURCE.get(file.origin, file.origin), applied_at=_file_time(path),
    )
    file.status = "processed"
    audit(session, actor_type="system", actor_name="intake", action="version.written",
          entity_type="candidate", entity_id=candidate.id,
          output={"version_id": version.id, "version_no": version.version_no, "resolution": resolution.action,
                  "extraction_status": status},
          latency_ms=int((time.monotonic() - started) * 1000), job_id=job.id)

    # step 10 and 8
    finish_candidate(session, resolution, candidate, version, built, file.original_name, job.id)
    enqueue_embed(session, version.id)
    if status == "ai_pending":
        queue.enqueue(session, "cv.reextract", {"version_id": version.id}, dedupe_key=f"cv.reextract:{version.id}",
                      max_attempts=10, run_after=datetime.now(timezone.utc) + timedelta(seconds=60))


@queue.register("cv.reextract")
def reextract(session: Session, job: Job) -> None:
    """The model was down when the CV arrived. Retry extraction on the stored text and upgrade the version."""
    version = session.get(CandidateVersion, job.payload["version_id"])
    if version is None or version.extraction_status == "ok":
        return
    text = version.search_text or ""
    result = extractor.extract(text, get_client())  # LLMUnavailable propagates -> retried with backoff
    built = build_profile(result.parsed, text, ai_used=True)
    apply_profile(version, built)
    version.extraction_status = "ok"
    add_identifiers(session, version.candidate_id, version.id, built.data)
    audit(session, actor_type="agent", actor_name="extractor", action="agent.extract", entity_type="candidate_version",
          entity_id=version.id, input=text, output={"upgraded_from": "ai_pending"}, model=result.model,
          prompt_version=extractor.PROMPT_VERSION, latency_ms=result.latency_ms, job_id=job.id)
    resolution = resolve(session, built.data, exclude_candidate_ids={version.candidate_id})
    if resolution.action in {"possible_match", "ambiguous"}:
        candidate = session.get(Candidate, version.candidate_id)
        assert candidate is not None
        finish_candidate(session, resolution, candidate, version, built, "this CV", job.id)
    elif built.low_confidence:
        _review(session, "low_confidence", "Check the details read from a CV", "; ".join(built.issues),
                "candidate", version.candidate_id)
    enqueue_embed(session, version.id, "reextract")
