"""Hard filters (SQL) and retrieval (pgvector) for workflow B, steps 5 and 6."""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.agents.requirement_parser import find_min_years
from app.config import settings
from app.core.models import Candidate, CandidateEmbedding, CandidateVersion
from app.llm.ollama import LLMError, get_client
from app.matching.scoring import Requirement
from app.parsing.normalise import canon_cert


@dataclass
class Retrieved:
    versions: list[CandidateVersion]
    similarity: dict[int, float]  # candidate_id -> 0..1
    mode: str  # semantic | keyword_fallback
    excluded_unknown_nationality: int = 0
    notes: list[str] = field(default_factory=list)


def eligible(session: Session, reqs: list[Requirement], min_years: int | None) -> tuple[list[int], int, list[str]]:
    """Candidate ids passing the must-have hard filters. Unknowns pass, except a stated nationality requirement."""
    V = CandidateVersion
    stmt = (
        select(Candidate.id)
        .join(V, V.id == Candidate.current_version_id)
        .where(Candidate.merged_into_id.is_(None))
    )
    notes: list[str] = []
    for r in (x for x in reqs if x.kind == "must"):
        if r.category == "certification":
            canon = canon_cert(r.text)
            stmt = stmt.where(or_(V.certifications.contains([canon]), V.search_text.ilike(f"%{canon}%")))
            notes.append(f"Must hold {canon}")
        elif r.category == "years_experience" and find_min_years(r.text) and not min_years:
            min_years = find_min_years(r.text)
    if min_years:
        stmt = stmt.where(or_(V.years_experience.is_(None), V.years_experience >= min_years))
        notes.append(f"At least {min_years} years (candidates whose years could not be calculated are kept)")
    excluded = 0
    nat = next((r for r in reqs if r.kind == "must" and r.category == "nationality"), None)
    if nat:
        before = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        stmt = stmt.where(V.nationality.ilike(f"%{nat.text.strip()}%"))
        after = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        excluded = before - after
        notes.append(f"Nationality: {nat.text} (only CVs that state it are considered)")
    return [r[0] for r in session.execute(stmt).all()], excluded, notes


def retrieve(session: Session, query_text: str, candidate_ids: list[int], top_k: int | None = None) -> Retrieved:
    """Top candidates by meaning (embeddings). Falls back to keyword ranking if the embedding model is down."""
    top_k = top_k or settings.retrieval_top_k
    if not candidate_ids:
        return Retrieved([], {}, "semantic")
    V = CandidateVersion
    mode, sims = "semantic", {}
    try:
        vector = get_client().embed([query_text], "query").vectors[0]
        distance = CandidateEmbedding.embedding.cosine_distance(vector)
        rows = session.execute(
            select(Candidate.id, func.min(distance).label("d"))
            .join(V, V.id == Candidate.current_version_id)
            .join(CandidateEmbedding, CandidateEmbedding.candidate_version_id == V.id)
            .where(Candidate.id.in_(candidate_ids))
            .group_by(Candidate.id)
            .order_by("d")
            .limit(top_k)
        ).all()
        sims = {cid: 1.0 - float(d) for cid, d in rows}
    except LLMError:
        mode = "keyword_fallback"
    def keyword_ids(pool: list[int]) -> list[int]:
        tsq = func.websearch_to_tsquery("english", " OR ".join(query_text.split()[:30]))
        rows = session.execute(
            select(Candidate.id)
            .join(V, V.id == Candidate.current_version_id)
            .where(Candidate.id.in_(pool), V.search_tsv.op("@@")(tsq))
            .order_by(func.ts_rank(V.search_tsv, tsq).desc())
            .limit(top_k)
        ).all()
        return [r[0] for r in rows]

    if not sims:
        mode = "keyword_fallback"
        sims = {cid: 0.0 for cid in keyword_ids(candidate_ids)}
    extra: list[int] = []
    waiting_note: list[str] = []
    if mode == "semantic":
        # Embedding jobs can queue behind a big ingest. Those people must not silently vanish from matching.
        embedded = set(session.scalars(
            select(Candidate.id).join(V, V.id == Candidate.current_version_id)
            .join(CandidateEmbedding, CandidateEmbedding.candidate_version_id == V.id)
            .where(Candidate.id.in_(candidate_ids))))
        waiting = [c for c in candidate_ids if c not in embedded]
        if waiting:
            extra = keyword_ids(waiting)
            waiting_note = [f"{len(waiting)} candidate(s) have not been embedded yet, so they were matched by keywords only"]
    ids = list(sims) + [c for c in extra if c not in sims]
    versions = session.scalars(
        select(V).join(Candidate, Candidate.current_version_id == V.id).where(Candidate.id.in_(ids or [-1]))
    ).all()
    by_cand = {v.candidate_id: v for v in versions}
    ordered = [by_cand[c] for c in ids if c in by_cand]
    note = ["Ranked by keywords because the AI embedding service was unavailable"] if mode == "keyword_fallback" else waiting_note
    return Retrieved(ordered, sims if mode == "semantic" else {}, mode, notes=note)
