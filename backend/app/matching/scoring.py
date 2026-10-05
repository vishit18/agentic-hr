"""Requirement checks and the transparent weighted score (workflow B, step 7). All code, no model.

Every requirement is checked against the candidate's structured profile first and the CV text second.
Results are "met", "unmet" or "unknown" (we cannot tell, for example when years could not be calculated).
The score is a weighted average of the components that apply; weights live in config.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.agents.requirement_parser import find_min_years
from app.config import settings
from app.core.models import CandidateVersion
from app.parsing.normalise import canon_cert, canon_skill
from app.parsing.text import squash

_STOP = {"with", "and", "the", "for", "from", "that", "have", "experience", "years", "year", "good", "strong",
         "knowledge", "skills", "skill", "ability", "excellent", "degree", "bachelor", "bachelors", "master",
         "masters", "of", "in", "a", "an", "to", "or", "as", "at", "senior", "junior"}


@dataclass
class Requirement:
    id: int | None
    kind: str  # must | nice
    category: str
    text: str


@dataclass
class ReqResult:
    req_id: int | None
    text: str
    kind: str
    category: str
    status: str  # met | unmet | unknown
    evidence: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"req_id": self.req_id, "text": self.text, "kind": self.kind, "category": self.category,
                "status": self.status, "evidence": self.evidence}


@dataclass
class Scored:
    score: float
    components: dict[str, float | None]
    results: list[ReqResult] = field(default_factory=list)


def tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9+#]+", squash(text)) if (len(w) >= 2 or w.isdigit()) and w not in _STOP]


def _in_cv(text: str, v: CandidateVersion) -> bool:
    return squash(text) in squash(v.search_text or "")


def _fraction_in_cv(text: str, v: CandidateVersion) -> float:
    words = tokens(text)
    if not words:
        return 0.0
    hay = squash(v.search_text or "")
    hits = sum(1 for w in words if re.search(rf"\b{re.escape(w)}", hay))
    return hits / len(words)


def evaluate(req: Requirement, v: CandidateVersion) -> ReqResult:
    base = dict(req_id=req.id, text=req.text, kind=req.kind, category=req.category)
    text = req.text
    data: dict[str, Any] = v.data or {}

    def res(status: str, evidence: str | None = None) -> ReqResult:
        return ReqResult(status=status, evidence=evidence, **base)  # type: ignore[arg-type]

    if req.category == "certification":
        canon = canon_cert(text)
        certs = {c.lower() for c in (v.certifications or [])}
        if canon.lower() in certs:
            return res("met", f"Certificate on CV: {canon}")
        if _in_cv(canon, v) or _in_cv(text, v):
            return res("met", "Mentioned in the CV")
        return res("unmet")

    if req.category == "years_experience":
        n = find_min_years(text)
        if n is None:
            return res("unknown")
        m = re.search(r"(?:years?|yrs?)(?: of experience)?\s+(?:in|as|within|working in)\s+(.+)", text, re.IGNORECASE)
        if m:
            area = squash(m.group(1))
            for key, years in (data.get("industry_years") or {}).items():
                if key in area or area in key:
                    return res("met" if years >= n else "unmet", f"{years:g} years in {key}")
            titles = " ".join(v.titles or [])
            if v.years_experience is not None and v.years_experience >= n and (area in titles or _in_cv(area, v)):
                return res("met", f"{v.years_experience:g} years total, {area} mentioned")
            return res("unknown" if v.years_experience is None else "unmet",
                       None if v.years_experience is None else f"{v.years_experience:g} years total, none clearly in {area}")
        if v.years_experience is None:
            return res("unknown")
        return res("met" if v.years_experience >= n else "unmet", f"{v.years_experience:g} years of experience")

    if req.category == "job_title":
        want = set(tokens(text))
        for t in (v.titles or []):
            if want and len(want & set(tokens(t))) / len(want) >= 0.5:
                return res("met", f"Has held the title '{t}'")
        return res("unmet")

    if req.category == "industry":
        want = squash(text)
        if any(want in i or i in want for i in (v.industries or [])):
            return res("met", "Industry experience on CV")
        return res("met", "Mentioned in the CV") if _in_cv(want, v) else res("unmet")

    if req.category == "skill":
        skill = canon_skill(text)
        if skill in (v.skills or []):
            return res("met", f"Skill on CV: {skill}")
        return res("met", "Mentioned in the CV") if _in_cv(text, v) else res("unmet")

    if req.category == "education":
        edu = squash(" ".join([v.education_text or "", *(v.qualifications or [])]))
        want = tokens(text)
        if want and sum(1 for w in want if w in edu) / len(want) >= 0.5:
            return res("met", "Education on CV")
        return res("met", "Mentioned in the CV") if _fraction_in_cv(text, v) >= 0.6 else res("unmet")

    if req.category == "nationality":
        if not v.nationality:
            return res("unknown", "The CV does not state a nationality")
        return res("met" if squash(text) in squash(v.nationality) or squash(v.nationality) in squash(text) else "unmet")

    # language and other: key words must appear in the CV
    if not tokens(text):
        return res("unknown")
    frac = _fraction_in_cv(text, v)
    if frac >= 0.99:
        return res("met", "Mentioned in the CV")
    return res("unmet") if frac < 0.5 else res("unknown", "Partly mentioned in the CV")


def _fraction(results: list[ReqResult]) -> float | None:
    if not results:
        return None
    pts = sum(1.0 if r.status == "met" else 0.5 if r.status == "unknown" else 0.0 for r in results)
    return pts / len(results)


def title_overlap(job_title: str | None, v: CandidateVersion) -> float | None:
    if not job_title:
        return None
    want = set(tokens(job_title))
    if not want:
        return None
    return max((len(want & set(tokens(t))) / len(want) for t in (v.titles or [])), default=0.0)


def score_candidate(
    reqs: list[Requirement], v: CandidateVersion, *, job_title: str | None, min_years: float | None,
    similarity: float | None,
) -> Scored:
    results = [evaluate(r, v) for r in reqs]
    must = [r for r in results if r.kind == "must"]
    nice = [r for r in results if r.kind == "nice"]
    years: float | None = None
    if min_years:
        years = 0.5 if v.years_experience is None else min(1.0, v.years_experience / min_years)
    comps: dict[str, float | None] = {
        "must_haves": _fraction(must),
        "nice_to_haves": _fraction(nice),
        "years": years,
        "title": title_overlap(job_title, v),
        "similarity": None if similarity is None else max(0.0, min(1.0, similarity)),
    }
    weights = {"must_haves": settings.score_w_must, "nice_to_haves": settings.score_w_nice,
               "years": settings.score_w_years, "title": settings.score_w_title,
               "similarity": settings.score_w_similarity}
    used = {k: weights[k] for k, c in comps.items() if c is not None}
    total_w = sum(used.values())
    score = 100 * sum(weights[k] * (comps[k] or 0.0) for k in used) / total_w if total_w else 0.0
    return Scored(score=round(score, 1), components=comps, results=results)
