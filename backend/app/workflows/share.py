"""What a company receives when a recruiter approves a shortlist (workflow B, step 11).

Shared: name, a short summary the recruiter has read and may edit, relevant experience, skills, certificates,
and the CV or contact details only if the recruiter ticks them. Kept internal: scores, gaps, notes, other
applications, nationality and anything protected.
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.models import CandidateVersion, File, ShortlistItem


@dataclass
class SharedCandidate:
    item_id: int
    candidate_id: int
    name: str
    summary: str
    experience: list[str]
    skills: list[str]
    certifications: list[str]
    include_cv: bool = False
    include_contact: bool = False
    contact: list[str] = field(default_factory=list)
    cv_name: str | None = None
    cv_path: Path | None = None


def company_view(item: ShortlistItem, version: CandidateVersion, file: File | None, *, summary: str | None = None,
                 include_cv: bool = False, include_contact: bool = False) -> SharedCandidate:
    d: dict[str, Any] = version.data or {}
    exp = [f"{e.get('title') or 'Role'} at {e.get('company') or 'an employer'} "
           f"({(e.get('start') or '?')[:4]}-{'present' if e.get('is_current') else (e.get('end') or '?')[:4]})"
           for e in (d.get("experience") or [])[:4]]
    contact = [*d.get("emails", [])[:1], *d.get("phones", [])[:1]] if include_contact else []
    return SharedCandidate(
        item_id=item.id, candidate_id=item.candidate_id, name=version.full_name or "Candidate",
        summary=(summary if summary is not None else (item.explanation or {}).get("summary", "")).strip(),
        experience=exp, skills=(d.get("skills") or [])[:10], certifications=(d.get("certifications") or [])[:8],
        include_cv=include_cv and file is not None, include_contact=include_contact, contact=contact,
        cv_name=file.original_name if file and include_cv else None,
        cv_path=Path(file.stored_path) if file and include_cv else None,
    )


def render_text(company: str, job_title: str | None, reference: str, people: list[SharedCandidate], sender: str) -> str:
    lines = [f"Hello,", "", f"Here is the shortlist for {job_title or 'your role'} (reference {reference}).", ""]
    for i, p in enumerate(people, start=1):
        lines += [f"{i}. {p.name}", f"   {p.summary}" if p.summary else ""]
        if p.experience:
            lines.append("   Experience: " + "; ".join(p.experience))
        if p.skills:
            lines.append("   Skills: " + ", ".join(p.skills))
        if p.certifications:
            lines.append("   Certificates: " + ", ".join(p.certifications))
        if p.contact:
            lines.append("   Contact: " + ", ".join(p.contact))
        if p.cv_name:
            lines.append(f"   CV attached: {p.cv_name}")
        lines.append("")
    lines += ["Kind regards,", sender, "Cadence HR"]
    return "\n".join(x for x in lines if x is not None)


def render_html(company: str, job_title: str | None, reference: str, people: list[SharedCandidate], sender: str) -> str:
    e = html.escape
    parts = [f"<p>Hello,</p><p>Here is the shortlist for <strong>{e(job_title or 'your role')}</strong> "
             f"(reference {e(reference)}).</p>"]
    for i, p in enumerate(people, start=1):
        parts.append(f"<h3>{i}. {e(p.name)}</h3>")
        if p.summary:
            parts.append(f"<p>{e(p.summary)}</p>")
        if p.experience:
            parts.append("<p><strong>Experience:</strong> " + e("; ".join(p.experience)) + "</p>")
        if p.skills:
            parts.append("<p><strong>Skills:</strong> " + e(", ".join(p.skills)) + "</p>")
        if p.certifications:
            parts.append("<p><strong>Certificates:</strong> " + e(", ".join(p.certifications)) + "</p>")
        if p.contact:
            parts.append("<p><strong>Contact:</strong> " + e(", ".join(p.contact)) + "</p>")
        if p.cv_name:
            parts.append(f"<p><em>CV attached: {e(p.cv_name)}</em></p>")
    parts.append(f"<p>Kind regards,<br>{e(sender)}<br>Cadence HR</p>")
    return "\n".join(parts)
