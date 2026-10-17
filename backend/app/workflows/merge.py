"""Merging profiles and unlinking CVs. Always a person's action, always reversible.

Each action stores a snapshot in its audit row (action "candidate.merged" / "candidate.version_unlinked").
Undo reads that snapshot and puts everything back, then writes a "...undone" audit row.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.models import (
    AuditLog,
    Candidate,
    CandidateIdentifier,
    CandidateVersion,
    ReviewItem,
    ShortlistItem,
    User,
)


class MergeError(Exception):
    """Shown to the person as a plain-English reason."""


def _stamp(v: CandidateVersion) -> datetime:
    return v.applied_at or v.created_at or datetime.min.replace(tzinfo=timezone.utc)


def latest_version(session: Session, candidate_id: int) -> CandidateVersion | None:
    versions = list(session.scalars(select(CandidateVersion).where(CandidateVersion.candidate_id == candidate_id)))
    return max(versions, key=lambda v: (_stamp(v), v.version_no), default=None)


def _user_audit(session: Session, user: User, action: str, candidate_id: int, output: dict[str, Any]) -> AuditLog:
    return audit(session, actor_type="user", actor_id=user.id, actor_name=user.email, action=action,
                 entity_type="candidate", entity_id=candidate_id, output=output)


def already_undone(session: Session, audit_id: int) -> bool:
    for row in session.scalars(select(AuditLog).where(AuditLog.action.in_(("candidate.merge_undone", "candidate.unlink_undone")))):
        if (row.output or {}).get("of") == audit_id:
            return True
    return False


# ---------------------------------------------------------------- merge

def merge_candidates(session: Session, keep_id: int, drop_id: int, user: User) -> AuditLog:
    if keep_id == drop_id:
        raise MergeError("Choose two different profiles.")
    keep, drop = session.get(Candidate, keep_id), session.get(Candidate, drop_id)
    if keep is None or drop is None:
        raise MergeError("One of the profiles no longer exists.")
    if keep.merged_into_id or drop.merged_into_id:
        raise MergeError("One of these profiles has already been merged into another.")

    keep_versions = list(session.scalars(select(CandidateVersion).where(CandidateVersion.candidate_id == keep.id)))
    drop_versions = sorted(session.scalars(select(CandidateVersion).where(CandidateVersion.candidate_id == drop.id)),
                           key=lambda v: v.version_no)
    next_no = max((v.version_no for v in keep_versions), default=0)
    moved = []
    for v in drop_versions:
        next_no += 1
        moved.append({"id": v.id, "version_no_before": v.version_no})
        v.candidate_id, v.version_no = keep.id, next_no
    identifiers = list(session.scalars(select(CandidateIdentifier).where(CandidateIdentifier.candidate_id == drop.id)))
    for i in identifiers:
        i.candidate_id = keep.id
    snapshot = {
        "keep_id": keep.id, "drop_id": drop.id,
        "keep_current_before": keep.current_version_id, "drop_current_before": drop.current_version_id,
        "moved_versions": moved, "moved_identifiers": [i.id for i in identifiers],
    }
    session.flush()
    drop.merged_into_id = keep.id
    latest = latest_version(session, keep.id)
    keep.current_version_id = latest.id if latest else keep.current_version_id

    resolved = []
    for item in session.scalars(select(ReviewItem).where(ReviewItem.type == "possible_duplicate", ReviewItem.status == "open")):
        ids = {item.entity_id, *((item.payload or {}).get("existing_candidate_ids") or [])}
        if keep.id in ids and drop.id in ids:
            item.status, item.resolved_by, item.resolved_at, item.resolution = "resolved", user.id, datetime.now(timezone.utc), "merged"
            resolved.append(item.id)
    snapshot["resolved_review_items"] = resolved
    return _user_audit(session, user, "candidate.merged", keep.id, {"snapshot": snapshot, "drop_id": drop.id})


def undo_merge(session: Session, audit_id: int, user: User) -> AuditLog:
    row = session.get(AuditLog, audit_id)
    if row is None or row.action != "candidate.merged":
        raise MergeError("That merge could not be found.")
    if already_undone(session, audit_id):
        raise MergeError("This merge has already been undone.")
    snap = (row.output or {})["snapshot"]
    keep, drop = session.get(Candidate, snap["keep_id"]), session.get(Candidate, snap["drop_id"])
    if keep is None or drop is None or drop.merged_into_id != keep.id:
        raise MergeError("The profiles have changed since the merge, so it cannot be undone automatically.")
    for m in snap["moved_versions"]:
        v = session.get(CandidateVersion, m["id"])
        if v is not None:
            v.candidate_id, v.version_no = drop.id, m["version_no_before"]
    for iid in snap["moved_identifiers"]:
        ident = session.get(CandidateIdentifier, iid)
        if ident is not None:
            ident.candidate_id = drop.id
    session.flush()
    drop.merged_into_id = None
    drop.current_version_id = snap["drop_current_before"]
    latest = latest_version(session, keep.id)
    keep.current_version_id = latest.id if latest else snap["keep_current_before"]
    for rid in snap.get("resolved_review_items", []):
        item = session.get(ReviewItem, rid)
        if item is not None and item.resolution == "merged":
            item.status, item.resolved_by, item.resolved_at, item.resolution = "open", None, None, None
    return _user_audit(session, user, "candidate.merge_undone", keep.id, {"of": audit_id, "drop_id": drop.id})


# ---------------------------------------------------------------- unlink one CV from a profile

def unlink_version(session: Session, version_id: int, user: User) -> AuditLog:
    v = session.get(CandidateVersion, version_id)
    if v is None:
        raise MergeError("That CV could not be found.")
    old = session.get(Candidate, v.candidate_id)
    others = list(session.scalars(select(CandidateVersion).where(
        CandidateVersion.candidate_id == v.candidate_id, CandidateVersion.id != v.id)))
    if old is None or not others:
        raise MergeError("This is the only CV on the profile, so there is nothing to unlink it from.")
    new = Candidate(source=old.source, source_detail=old.source_detail, created_by=user.id)
    session.add(new)
    session.flush()
    snap = {"version_id": v.id, "old_candidate_id": old.id, "new_candidate_id": new.id,
            "version_no_before": v.version_no, "auto_linked_before": v.auto_linked,
            "old_current_before": old.current_version_id}
    v.candidate_id, v.version_no, v.auto_linked = new.id, 1, False
    new.current_version_id = v.id
    # The CV's own contact details go with it. Left behind, they would let the next CV from that person
    # link to the wrong profile.
    moved = list(session.scalars(select(CandidateIdentifier).where(
        CandidateIdentifier.candidate_version_id == v.id, CandidateIdentifier.candidate_id == old.id)))
    for ident in moved:
        ident.candidate_id = new.id
    have = {(i.kind, i.value) for i in moved}
    created = []
    for kind, values in (("email", (v.data or {}).get("emails") or []), ("phone", (v.data or {}).get("phones") or [])):
        for value in values:
            if (kind, value) in have:
                continue
            ident = CandidateIdentifier(candidate_id=new.id, candidate_version_id=v.id, kind=kind, value=value)
            session.add(ident)
            session.flush()
            created.append(ident.id)
    snap["created_identifiers"] = created
    snap["moved_identifiers"] = [i.id for i in moved]
    session.flush()
    latest = latest_version(session, old.id)
    old.current_version_id = latest.id if latest else None
    return _user_audit(session, user, "candidate.version_unlinked", old.id, {"snapshot": snap})


def undo_unlink(session: Session, audit_id: int, user: User) -> AuditLog:
    row = session.get(AuditLog, audit_id)
    if row is None or row.action != "candidate.version_unlinked":
        raise MergeError("That change could not be found.")
    if already_undone(session, audit_id):
        raise MergeError("This has already been undone.")
    snap = (row.output or {})["snapshot"]
    v, old, new = (session.get(CandidateVersion, snap["version_id"]), session.get(Candidate, snap["old_candidate_id"]),
                   session.get(Candidate, snap["new_candidate_id"]))
    if v is None or old is None or new is None or v.candidate_id != new.id:
        raise MergeError("The profiles have changed since, so this cannot be undone automatically.")
    extra = session.scalar(select(CandidateVersion.id).where(CandidateVersion.candidate_id == new.id, CandidateVersion.id != v.id))
    on_list = session.scalar(select(ShortlistItem.id).where(ShortlistItem.candidate_id == new.id))
    if extra or on_list:
        raise MergeError("The new profile has been used since (another CV or a shortlist), so it cannot be undone automatically.")
    v.candidate_id, v.version_no, v.auto_linked = old.id, snap["version_no_before"], snap["auto_linked_before"]
    for iid in snap["created_identifiers"]:
        ident = session.get(CandidateIdentifier, iid)
        if ident is not None:
            session.delete(ident)
    for iid in snap.get("moved_identifiers", []):  # older audit rows have none
        ident = session.get(CandidateIdentifier, iid)
        if ident is not None:
            ident.candidate_id = old.id
    session.flush()
    new.current_version_id = None
    session.flush()
    session.delete(new)
    session.flush()
    latest = latest_version(session, old.id)
    old.current_version_id = latest.id if latest else snap["old_current_before"]
    return _user_audit(session, user, "candidate.unlink_undone", old.id, {"of": audit_id})
