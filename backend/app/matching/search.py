"""Candidate search and filters (requirements 2.2 to 2.4).

Filters are plain SQL over the current version of each candidate. Free text combines Postgres full
text search (keywords, exact terms) with pgvector semantic search (meaning), merged by reciprocal rank.
Semantic search is optional: if the embedding model is down, full text still works and the page says so.
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session

from app.core.models import Candidate, CandidateEmbedding, CandidateVersion
from app.llm.ollama import LLMError, get_client
from app.parsing.normalise import canon_cert, canon_skill

PAGE_SIZE = 25
SEMANTIC_MAX_DISTANCE = 0.55  # cosine distance; farther than this is not "similar"
RRF_K = 60


@dataclass
class Filters:
    q: str = ""
    name: str = ""
    skills: list[str] = field(default_factory=list)
    certs: list[str] = field(default_factory=list)
    min_years: float | None = None
    max_years: float | None = None
    education: str = ""
    qualification: str = ""
    industry: str = ""
    title: str = ""
    nationality: str = ""
    source: str = ""

    def describe(self) -> str:
        """Words for the count line, for example 'with CFA and python'."""
        parts = [*self.certs, *self.skills]
        if self.industry:
            parts.append(f"experience in {self.industry}")
        if self.title:
            parts.append(f"the title '{self.title}'")
        if self.min_years is not None:
            parts.append(f"{self.min_years:g}+ years")
        if self.q:
            parts.append(f"'{self.q}'")
        return ("with " + ", ".join(parts)) if parts else ""


@dataclass
class SearchResult:
    rows: list[dict[str, Any]]
    total: int
    page: int
    semantic_used: bool
    semantic_note: str | None = None


def split_terms(value: str) -> list[str]:
    return [t.strip() for t in value.replace(";", ",").split(",") if t.strip()]


def _like(column: Any, value: str) -> Any:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return column.ilike(f"%{escaped}%", escape="\\")


def _array_text(column: Any) -> Any:
    return func.array_to_string(column, " | ")


def apply_filters(stmt: Select, f: Filters) -> Select:
    V = CandidateVersion
    conds: list[Any] = []
    if f.name:
        conds.append(_like(V.full_name, f.name))
    for s in f.skills:
        conds.append(V.skills.contains([canon_skill(s)]))
    for c in f.certs:
        conds.append(or_(V.certifications.contains([canon_cert(c)]), _like(_array_text(V.certifications), c)))
    if f.min_years is not None:
        conds.append(V.years_experience >= f.min_years)
    if f.max_years is not None:
        conds.append(V.years_experience <= f.max_years)
    if f.education:
        conds.append(_like(V.education_text, f.education))
    if f.qualification:
        conds.append(_like(_array_text(V.qualifications), f.qualification))
    if f.industry:
        conds.append(_like(_array_text(V.industries), f.industry))
    if f.title:
        conds.append(or_(_like(V.current_title, f.title), _like(_array_text(V.titles), f.title)))
    if f.nationality:
        conds.append(_like(V.nationality, f.nationality))  # only candidates whose CV states it match
    if f.source:
        conds.append(Candidate.source == f.source)
    return stmt.where(and_(*conds)) if conds else stmt


def base_query(*cols: Any) -> Select:
    return (
        select(*cols)
        .select_from(Candidate)
        .join(CandidateVersion, CandidateVersion.id == Candidate.current_version_id)
        .where(Candidate.merged_into_id.is_(None))
    )


def _fulltext_ids(session: Session, q: str, limit: int = 300) -> list[int]:
    tsq = func.websearch_to_tsquery("english", q)
    rows = session.execute(
        select(Candidate.id)
        .join(CandidateVersion, CandidateVersion.id == Candidate.current_version_id)
        .where(Candidate.merged_into_id.is_(None), CandidateVersion.search_tsv.op("@@")(tsq))
        .order_by(func.ts_rank(CandidateVersion.search_tsv, tsq).desc())
        .limit(limit)
    ).all()
    return [r[0] for r in rows]


def _semantic_ids(session: Session, q: str, limit: int = 100) -> tuple[list[int], str | None]:
    try:
        vector = get_client().embed([q], "query").vectors[0]
    except LLMError:
        return [], "Meaning-based search is paused because the AI service is unavailable; showing keyword matches."
    distance = CandidateEmbedding.embedding.cosine_distance(vector)
    rows = session.execute(
        select(Candidate.id, func.min(distance).label("d"))
        .join(CandidateVersion, CandidateVersion.id == Candidate.current_version_id)
        .join(CandidateEmbedding, CandidateEmbedding.candidate_version_id == CandidateVersion.id)
        .where(Candidate.merged_into_id.is_(None))
        .group_by(Candidate.id)
        .having(func.min(distance) <= SEMANTIC_MAX_DISTANCE)
        .order_by("d")
        .limit(limit)
    ).all()
    return [r[0] for r in rows], None


def fuse(*ranked: list[int]) -> list[int]:
    """Reciprocal rank fusion: items ranked high in any list rise to the top."""
    scores: dict[int, float] = {}
    for ids in ranked:
        for rank, cid in enumerate(ids):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
    return sorted(scores, key=lambda c: scores[c], reverse=True)


def highlight(session: Session, version_ids: list[int], q: str) -> dict[int, str]:
    """Matching terms in context. Text is escaped first, then only our own marker becomes <mark>."""
    if not version_ids or not q:
        return {}
    tsq = func.websearch_to_tsquery("english", q)
    rows = session.execute(
        select(
            CandidateVersion.id,
            func.ts_headline(
                "english", func.left(CandidateVersion.search_text, 20000), tsq,
                "StartSel=[[[, StopSel=]]], MaxFragments=2, MaxWords=22, MinWords=8, FragmentDelimiter= ... ",
            ),
        ).where(CandidateVersion.id.in_(version_ids))
    ).all()
    out: dict[int, str] = {}
    for vid, snippet in rows:
        if snippet and "[[[" in snippet:
            safe = html.escape(snippet).replace("[[[", "<mark>").replace("]]]", "</mark>")
            out[vid] = safe
    return out


def search(session: Session, f: Filters, page: int = 1) -> SearchResult:
    page = max(page, 1)
    V = CandidateVersion
    q = f.q.strip()
    semantic_used, note = False, None
    order_ids: list[int] | None = None

    if q:
        fts = _fulltext_ids(session, q)
        sem, note = _semantic_ids(session, q)
        semantic_used = bool(sem) or note is None
        order_ids = fuse(fts, sem)

    stmt = base_query(Candidate.id)
    stmt = apply_filters(stmt, f)
    if order_ids is not None:
        stmt = stmt.where(Candidate.id.in_(order_ids or [-1]))

    matching = [r[0] for r in session.execute(stmt).all()]
    total = len(matching)
    if order_ids is not None:
        rank = {cid: i for i, cid in enumerate(order_ids)}
        matching.sort(key=lambda c: rank[c])
        page_ids = matching[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
    else:
        ordered = session.execute(
            apply_filters(base_query(Candidate.id), f)
            .order_by(V.applied_at.desc().nulls_last(), Candidate.id.desc())
            .limit(PAGE_SIZE)
            .offset((page - 1) * PAGE_SIZE)
        ).all()
        page_ids = [r[0] for r in ordered]

    data = session.execute(
        base_query(
            Candidate.id, Candidate.source, V.id.label("version_id"), V.full_name, V.current_title,
            V.years_experience, V.skills, V.certifications, V.applied_at, V.auto_linked, V.extraction_status,
        ).where(Candidate.id.in_(page_ids or [-1]))
    ).all()
    by_id = {r.id: r for r in data}
    marks = highlight(session, [by_id[i].version_id for i in page_ids if i in by_id], q)
    rows = []
    for cid in page_ids:
        r = by_id.get(cid)
        if r is None:
            continue
        rows.append(
            {
                "id": r.id, "name": r.full_name, "title": r.current_title, "years": r.years_experience,
                "skills": (r.skills or [])[:5], "certs": r.certifications or [], "updated": r.applied_at,
                "source": r.source, "highlight": marks.get(r.version_id), "ai_pending": r.extraction_status == "ai_pending",
            }
        )
    return SearchResult(rows=rows, total=total, page=page, semantic_used=semantic_used, semantic_note=note)
