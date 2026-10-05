"""Identity resolution (workflow A, step 7). Pure code, deterministic and conservative.

1. Exact normalised email or phone  -> same person (auto-link, reversible, logged).
2. Identifiers hit two profiles     -> ambiguous, a human decides.
3. Name signals only                -> never auto-linked: "possible match" for the review queue.
The Adjudicator agent only ever sees the possible-match pairs, and only suggests.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.models import Candidate, CandidateIdentifier, CandidateVersion

NAME_FUZZY_CUTOFF = 88
_HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "eng", "engr", "prof", "sir"}
_COMPANY_NOISE = {"ltd", "limited", "llc", "inc", "co", "company", "corp", "corporation", "wll", "bsc", "spc", "plc", "the", "group", "holding", "holdings"}


@dataclass
class Resolution:
    action: str  # new | auto_link | ambiguous | possible_match
    candidate_id: int | None = None
    other_candidate_ids: list[int] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    score: float = 0.0


def norm_name(name: str | None) -> str:
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    tokens = [t for t in re.split(r"[^a-z]+", text) if t and t not in _HONORIFICS]
    return " ".join(tokens)


def norm_org(name: str | None) -> str:
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    tokens = [t for t in re.split(r"[^a-z0-9]+", text) if t and t not in _COMPANY_NOISE]
    return " ".join(tokens)


def _orgs(data: dict[str, Any], key: str, field_name: str) -> set[str]:
    return {o for o in (norm_org(x.get(field_name)) for x in data.get(key) or []) if o}


def resolve(
    session: Session,
    profile: dict[str, Any],
    *,
    exclude_candidate_ids: set[int] | None = None,
) -> Resolution:
    exclude = exclude_candidate_ids or set()

    # --- 1 and 2: exact identifiers ---
    wanted = [("email", e) for e in profile.get("emails") or []] + [("phone", p) for p in profile.get("phones") or []]
    if wanted:
        rows = session.execute(
            select(CandidateIdentifier.candidate_id, CandidateIdentifier.kind, CandidateIdentifier.value)
            .join(Candidate, Candidate.id == CandidateIdentifier.candidate_id)
            .where(Candidate.merged_into_id.is_(None))
            .where(CandidateIdentifier.kind.in_({k for k, _ in wanted}))
            .where(CandidateIdentifier.value.in_({v for _, v in wanted}))
        ).all()
        hits: dict[int, list[str]] = {}
        wanted_set = set(wanted)
        for cid, kind, value in rows:
            if cid in exclude or (kind, value) not in wanted_set:
                continue
            label = f"Same {kind}: {value}"
            if label not in hits.setdefault(cid, []):
                hits[cid].append(label)
        if len(hits) == 1:
            cid, evidence = next(iter(hits.items()))
            return Resolution("auto_link", cid, [], evidence, 1.0)
        if len(hits) > 1:
            ids = sorted(hits)
            evidence = [f"Profile {c}: " + "; ".join(hits[c]) for c in ids]
            return Resolution("ambiguous", ids[0], ids[1:], evidence, 1.0)

    # --- 3: names, never auto-linked ---
    name = norm_name(profile.get("name"))
    if len(name.split()) < 2:
        return Resolution("new")

    pool = session.execute(
        select(CandidateVersion.candidate_id, CandidateVersion.full_name, CandidateVersion.data)
        .join(Candidate, Candidate.current_version_id == CandidateVersion.id)
        .where(Candidate.merged_into_id.is_(None))
    ).all()
    pool = [r for r in pool if r.candidate_id not in exclude and len(norm_name(r.full_name).split()) >= 2]
    if not pool:
        return Resolution("new")

    names = [norm_name(r.full_name) for r in pool]
    my_orgs = _orgs(profile, "experience", "company")
    my_schools = _orgs(profile, "education", "institution")

    best: Resolution | None = None
    for _, score, idx in process.extract(name, names, scorer=fuzz.token_sort_ratio, score_cutoff=NAME_FUZZY_CUTOFF, limit=10):
        row = pool[idx]
        data = row.data or {}
        shared_orgs = my_orgs & _orgs(data, "experience", "company")
        shared_schools = my_schools & _orgs(data, "education", "institution")
        evidence = [f"Name {round(score)}% similar ('{row.full_name}')"]
        if shared_orgs:
            evidence.append("Same employer: " + ", ".join(sorted(shared_orgs)))
        if shared_schools:
            evidence.append("Same school: " + ", ".join(sorted(shared_schools)))
        exact_name = score >= 100
        if not (shared_orgs or shared_schools):
            if not exact_name:
                continue
            evidence.append("Same name only, no other shared details")
        combined = score + 10 * len(shared_orgs) + 10 * len(shared_schools)
        if best is None or combined > best.score:
            best = Resolution("possible_match", row.candidate_id, [], evidence, combined)
    return best or Resolution("new")
