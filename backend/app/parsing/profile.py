"""Validate and normalise extractor output into the stored profile (workflow A, step 6).

Everything the model says is checked against the CV text in code. Contacts always come from regex.
Years of experience are computed from dates here, never taken from the model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.agents.extractor import Extracted, guess_name
from app.parsing.normalise import (
    SKILL_ALIASES,
    canon_cert,
    canon_skill,
    dedupe_keep_order,
    find_emails,
    find_phones,
    parse_date,
    years_of_experience,
)
from app.parsing.text import squash

NATIONALITY_CUE = re.compile(r"\b(nationality|citizen(ship)?|national of)\b", re.IGNORECASE)


@dataclass
class BuiltProfile:
    data: dict[str, Any]
    columns: dict[str, Any]
    issues: list[str] = field(default_factory=list)
    low_confidence: bool = False


def _in_text(needle: str | None, haystack: str, min_len: int = 3) -> bool:
    n = squash(needle or "")
    return len(n) >= min_len and n in haystack


def _name_ok(name: str | None, haystack: str) -> bool:
    if not name:
        return False
    tokens = [t for t in re.split(r"[\s,]+", squash(name)) if len(t) >= 2]
    return bool(tokens) and all(t in haystack for t in tokens)


def build_profile(
    extracted: Extracted | None,
    text: str,
    *,
    ai_used: bool,
    today: date | None = None,
    region: str | None = None,
) -> BuiltProfile:
    today = today or date.today()
    haystack = squash(text)
    issues: list[str] = []
    ex = extracted or Extracted()

    # --- name ---
    name = ex.full_name.strip() if ex.full_name else None
    if not _name_ok(name, haystack):
        if name:
            issues.append("Name from the model was not found in the CV text")
        name = guess_name(text)
    if not name:
        issues.append("No name could be found")

    # --- contacts: code only ---
    emails = find_emails(text)
    phones = find_phones(text, region)

    # --- experience ---
    experience: list[dict[str, Any]] = []
    ranges: list[tuple[date, date]] = []
    industry_ranges: dict[str, list[tuple[date, date]]] = {}
    dropped = 0
    for e in ex.experience:
        verified = _in_text(e.snippet, haystack, 6) or (_in_text(e.title, haystack) and _in_text(e.company, haystack))
        if not verified:
            dropped += 1
            continue
        start = parse_date(e.start, today=today)
        end = parse_date(e.end, end=True, today=today) if e.end else (today if start else None)
        industry = e.industry.strip().lower() if e.industry else None
        if start and end and end >= start:
            ranges.append((start, end))
            if industry:
                industry_ranges.setdefault(industry, []).append((start, end))
        elif e.start or e.end:
            issues.append(f"Could not read dates for '{e.title}'")
        experience.append(
            {
                "title": e.title.strip(),
                "company": e.company.strip(),
                "start": start.isoformat() if start else None,
                "end": end.isoformat() if end else None,
                "is_current": bool(e.end and e.end.strip().lower() in {"present", "current", "now", "ongoing", "to date", "till date"}),
                "industry": industry,
                "description": (e.description or "").strip() or None,
                "snippet": e.snippet.strip(),
            }
        )
    experience.sort(key=lambda x: x["end"] or "", reverse=True)

    # --- education ---
    education: list[dict[str, Any]] = []
    for ed in ex.education:
        verified = _in_text(ed.snippet, haystack, 6) or _in_text(ed.institution, haystack)
        if not verified:
            dropped += 1
            continue
        education.append(
            {
                "institution": ed.institution.strip(),
                "degree": (ed.degree or "").strip() or None,
                "field": (ed.field or "").strip() or None,
                "start": ed.start,
                "end": ed.end,
                "snippet": ed.snippet.strip(),
            }
        )

    # --- nationality: only if the CV states it explicitly ---
    nationality = None
    nationality_snippet = None
    if (
        ex.nationality
        and ex.nationality_snippet
        and _in_text(ex.nationality_snippet, haystack, 6)
        and NATIONALITY_CUE.search(ex.nationality_snippet)
        and squash(ex.nationality) in squash(ex.nationality_snippet)
    ):
        nationality = ex.nationality.strip()
        nationality_snippet = ex.nationality_snippet.strip()

    # --- skills, certs, qualifications: must appear in the CV, then canonicalised ---
    skills = dedupe_keep_order([canon_skill(s) for s in ex.skills if _in_text(s, haystack, 2)])
    certs = dedupe_keep_order([canon_cert(c) for c in ex.certifications if _in_text(c, haystack, 2)])
    quals = dedupe_keep_order([q.strip() for q in ex.qualifications if _in_text(q, haystack, 2)])
    if not ai_used:
        # Code-only fallback: known skills named in the text
        for canon, aliases in SKILL_ALIASES.items():
            if any(re.search(rf"\b{re.escape(a)}\b", haystack) for a in aliases):
                skills.append(canon)
        skills = dedupe_keep_order(skills)

    # --- derived ---
    years = years_of_experience(ranges) if ranges else None
    industry_years = {k: years_of_experience(v) for k, v in industry_ranges.items()}
    current = next((x for x in experience if x["is_current"]), experience[0] if experience else None)
    titles = dedupe_keep_order([x["title"].lower() for x in experience])
    education_text = "; ".join(
        " ".join(filter(None, [d["degree"], d["field"], d["institution"]])) for d in education
    ) or None

    total_claimed = len(ex.experience) + len(ex.education)
    low = (
        not name
        or (ai_used and not experience and not education)
        or (total_claimed >= 2 and dropped / total_claimed >= 0.5)
    )
    if dropped:
        issues.append(f"{dropped} item(s) from the model were not found in the CV and were dropped")

    data: dict[str, Any] = {
        "name": name,
        "emails": emails,
        "phones": phones,
        "location": (ex.location or "").strip() or None,
        "summary": (ex.summary or "").strip() or None,
        "nationality": nationality,
        "nationality_snippet": nationality_snippet,
        "experience": experience,
        "education": education,
        "skills": skills,
        "certifications": certs,
        "qualifications": quals,
        "years_experience": years,
        "industry_years": industry_years,
        "issues": issues,
    }
    columns: dict[str, Any] = {
        "full_name": name,
        "current_title": current["title"] if current else None,
        "years_experience": years,
        "location": data["location"],
        "nationality": nationality,
        "skills": [s.lower() for s in skills],
        "certifications": certs,
        "qualifications": quals,
        "industries": sorted(industry_years),
        "titles": titles,
        "education_text": education_text,
        "search_text": text,
    }
    return BuiltProfile(data=data, columns=columns, issues=issues, low_confidence=bool(low))
