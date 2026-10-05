"""Add a sourced candidate (LinkedIn, referral ...) from the form, with the same duplicate check as CV intake.

A person confirms before anything is saved when the candidate may already exist.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.agents.extractor import Extracted
from app.core.audit import audit
from app.core.models import Candidate, CandidateVersion, User
from app.dedup.identity import Resolution, resolve
from app.parsing.profile import BuiltProfile, build_profile
from app.workflows.cv_embed import enqueue_embed
from app.workflows.cv_intake import finish_candidate, write_version

SOURCES = ["LinkedIn", "Referral", "Other approved source"]


@dataclass
class ManualInput:
    name: str
    email: str = ""
    phone: str = ""
    source: str = "LinkedIn"
    source_detail: str = ""
    location: str = ""


def build_manual_profile(inp: ManualInput) -> BuiltProfile:
    text = "\n".join(x for x in [inp.name, inp.email, inp.phone, inp.location, inp.source_detail] if x.strip())
    built = build_profile(Extracted(full_name=inp.name.strip(), location=inp.location.strip() or None),
                          text, ai_used=False)
    built.data["added_manually"] = True
    return built


def check_existing(session: Session, inp: ManualInput) -> tuple[BuiltProfile, Resolution]:
    built = build_manual_profile(inp)
    return built, resolve(session, built.data)


def create_manual(session: Session, inp: ManualInput, user: User) -> tuple[Candidate, CandidateVersion, Resolution]:
    built, resolution = check_existing(session, inp)
    candidate, version = write_version(
        session, resolution, built, "ok", None,
        source=inp.source.strip() or "manual", source_detail=inp.source_detail.strip() or None,
        applied_at=datetime.now(timezone.utc), created_by=user.id,
    )
    finish_candidate(session, resolution, candidate, version, built, "a manually added candidate", None)
    audit(session, actor_type="user", actor_id=user.id, actor_name=user.email, action="candidate.added_manually",
          entity_type="candidate", entity_id=candidate.id,
          output={"source": inp.source, "resolution": resolution.action, "version_id": version.id})
    enqueue_embed(session, version.id)
    return candidate, version, resolution
