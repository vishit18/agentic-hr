"""Workflow B steps 5-9: filter, retrieve, score, explain, write the shortlist.

Ranking is code. The Matcher agent only explains; it cannot change the order.
Items a person removed stay removed; items a person added by hand are kept when the shortlist is rebuilt.
"""
from __future__ import annotations

import time
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.agents import matcher
from app.config import settings
from app.core.audit import audit
from app.core.models import Request, RequestRequirement, ShortlistItem
from app.llm.ollama import LLMError, get_client
from app.matching.retrieval import eligible, retrieve
from app.matching.scoring import Requirement, Scored, score_candidate


def requirements_of(session: Session, request_id: int) -> list[RequestRequirement]:
    return list(session.scalars(
        select(RequestRequirement).where(RequestRequirement.request_id == request_id).order_by(RequestRequirement.id)
    ))


def to_reqs(rows: list[RequestRequirement]) -> list[Requirement]:
    return [Requirement(r.id, r.kind, r.category, r.text) for r in rows]


def query_text(request: Request, reqs: list[Requirement]) -> str:
    must = "; ".join(r.text for r in reqs if r.kind == "must")
    nice = "; ".join(r.text for r in reqs if r.kind == "nice")
    return f"{request.job_title or ''}. Required: {must}. Preferred: {nice}. {(request.description or '')[:600]}".strip()


def breakdown(scored: Scored, mode: str) -> dict[str, Any]:
    return {
        "components": scored.components,
        "requirements": [r.as_dict() for r in scored.results],
        "weights": {"must": settings.score_w_must, "nice": settings.score_w_nice, "years": settings.score_w_years,
                    "title": settings.score_w_title, "similarity": settings.score_w_similarity},
        "retrieval": mode,
    }


def build_shortlist(session: Session, request: Request, job_id: int | None = None) -> dict[str, Any]:
    rows = requirements_of(session, request.id)
    reqs = to_reqs(rows)
    min_years = (request.data or {}).get("min_years")
    ids, excluded_nat, notes = eligible(session, reqs, min_years)
    result = retrieve(session, query_text(request, reqs), ids)
    notes += result.notes

    existing = list(session.scalars(select(ShortlistItem).where(ShortlistItem.request_id == request.id)))
    removed = {i.candidate_id for i in existing if i.status == "removed"}
    manual = {i.candidate_id: i for i in existing if (i.explanation or {}).get("manual") and i.status != "removed"}
    session.execute(delete(ShortlistItem).where(
        ShortlistItem.request_id == request.id, ShortlistItem.status == "suggested",
        ShortlistItem.candidate_id.notin_(list(manual) or [-1]),
    ))

    scored = []
    for v in result.versions:
        if v.candidate_id in removed or v.candidate_id in manual:
            continue
        s = score_candidate(reqs, v, job_title=request.job_title, min_years=min_years,
                            similarity=result.similarity.get(v.candidate_id) if result.mode == "semantic" else None)
        scored.append((s, v))
    scored.sort(key=lambda t: t[0].score, reverse=True)
    top = scored[: settings.shortlist_explain_top]

    must_txt = [r.text for r in reqs if r.kind == "must"]
    nice_txt = [r.text for r in reqs if r.kind == "nice"]
    explained_by_model = 0
    start_rank = len(manual) + 1
    for offset, (s, v) in enumerate(top):
        met = [r.text for r in s.results if r.status == "met"]
        unmet = [r.text for r in s.results if r.status == "unmet"]
        expl: dict[str, Any]
        started = time.monotonic()
        try:
            pack = matcher.evidence_pack(v.data or {}, must_txt, nice_txt, met, unmet)
            out = matcher.explain(pack, get_client())
            cleaned, dropped = matcher.verify(out.parsed, v.search_text or "")
            expl = {"summary": cleaned.summary, "strengths": [x.model_dump() for x in cleaned.strengths],
                    "gaps": cleaned.gaps, "source": "model", "model": out.model,
                    "prompt_version": matcher.PROMPT_VERSION, "unverified_quotes_dropped": dropped}
            explained_by_model += 1
            audit(session, actor_type="agent", actor_name="matcher", action="agent.matcher",
                  entity_type="request", entity_id=request.id, input=pack,
                  output={"candidate_id": v.candidate_id, "strengths": len(cleaned.strengths), "dropped": dropped},
                  model=out.model, prompt_version=matcher.PROMPT_VERSION, latency_ms=out.latency_ms, job_id=job_id)
        except LLMError:
            t = matcher.template_summary(v.full_name, met, unmet, v.years_experience)
            expl = {"summary": t.summary, "strengths": [], "gaps": t.gaps, "source": "template"}
            audit(session, actor_type="system", actor_name="matcher-fallback", action="agent.matcher",
                  entity_type="request", entity_id=request.id,
                  output={"candidate_id": v.candidate_id, "fallback": "template"},
                  latency_ms=int((time.monotonic() - started) * 1000), job_id=job_id)
        session.add(ShortlistItem(
            request_id=request.id, candidate_id=v.candidate_id, rank=start_rank + offset,
            agent_rank=start_rank + offset, score=s.score, score_breakdown=breakdown(s, result.mode),
            explanation=expl, status="suggested",
        ))
    session.flush()
    return {"considered": len(ids), "retrieved": len(result.versions), "listed": len(top),
            "explained_by_model": explained_by_model, "retrieval": result.mode, "notes": notes,
            "excluded_unknown_nationality": excluded_nat}
