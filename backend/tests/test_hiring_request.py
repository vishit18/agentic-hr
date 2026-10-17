"""Workflow B end to end with a fake model client."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.agents.matcher import Explanation, Strength
from app.agents.requirement_parser import ParsedRequest, Req
from app.config import settings
from app.core import queue
from app.core.models import (
    AuditLog,
    Candidate,
    CandidateEmbedding,
    CandidateVersion,
    Job,
    Request,
    RequestRequirement,
    ReviewItem,
    ShortlistItem,
    User,
)
from app.llm.ollama import ChatResult, EmbedResult, LLMUnavailable
from app.matching import retrieval
from app.matching.scoring import Requirement, evaluate, score_candidate
from app.worker import run_one
from app.workflows import hiring_request as hr
from tests.test_candidates_ui import add_candidate

DIM = settings.embedding_dim
NEAR = [1.0] + [0.0] * (DIM - 1)
FAR = [0.0] * (DIM - 1) + [1.0]


class FakeClient:
    def __init__(self) -> None:
        self.up = True
        self.parsed = ParsedRequest(
            job_title="Senior Accountant", min_years_experience=5,
            must_have=[Req(text="CFA", category="certification"), Req(text="5 years of experience", category="years_experience")],
            nice_to_have=[Req(text="Excel", category="skill")],
        )
        self.quote = "Accountant, Acme, 2018"

    def chat(self, model, system, user, schema):
        if not self.up:
            raise LLMUnavailable("down")
        if schema is ParsedRequest:
            return ChatResult(self.parsed, model, 3)
        return ChatResult(Explanation(
            summary="Strong match with relevant banking experience.",
            strengths=[Strength(text="Held an accountant role", quote=self.quote),
                       Strength(text="Invented claim", quote="Chief Wizard at Hogwarts 1999")],
            gaps=["No audit certificate shown"]), model, 4)

    def embed(self, texts, kind, model=None):
        if not self.up:
            raise LLMUnavailable("down")
        return EmbedResult([NEAR for _ in texts], "nomic-embed-text", 2)


@pytest.fixture()
def env(db, session_factory, monkeypatch):
    client = FakeClient()
    for mod in (hr, retrieval):
        monkeypatch.setattr(mod, "get_client", lambda: client)
    from app.matching import shortlist
    monkeypatch.setattr(shortlist, "get_client", lambda: client)
    saved = dict(queue.HANDLERS)

    class E:
        pass

    e = E()
    e.db, e.sf, e.client = db, session_factory, client

    def drain():
        for _ in range(50):
            if not run_one(session_factory, "t"):
                return
        raise AssertionError("queue never drained")

    e.drain = drain
    yield e
    queue.HANDLERS.update(saved)


def with_embedding(db, cand, vec):
    db.add(CandidateEmbedding(candidate_version_id=cand.current_version_id, kind="profile", embedding=vec, model="m"))
    db.commit()


def recruiters(db, n=2):
    out = []
    for i in range(n):
        u = User(email=f"rec{i}@t.local", name=f"Rec {i}", password_hash="x", role="recruiter")
        db.add(u)
        out.append(u)
    db.commit()
    return out


def base_input(**kw):
    d = dict(company="Globex", contact_name="Gina", contact_email="gina@globex.test", job_title="Senior Accountant",
             location="Manama", description="We need a senior accountant with CFA and 5 years of experience.",
             must_text="CFA\n5 years of experience", nice_text="Excel")
    d.update(kw)
    return hr.RequestInput(**d)


def seed_candidates(db):
    good = add_candidate(db, "Alice Good", certs=["CFA"], skills=["microsoft excel"], years=9, title="Senior Accountant",
                         industries=["banking"], text="Alice Good. Accountant, Acme, 2018. CFA charterholder. Excel.")
    junior = add_candidate(db, "Bob Junior", certs=["CFA"], years=1, title="Accountant", text="Bob Junior. CFA. Accountant, Beta, 2024")
    nocert = add_candidate(db, "Cara NoCert", certs=[], years=10, title="Accountant", text="Cara NoCert. Accountant, Gamma")
    for c, vec in ((good, NEAR), (junior, NEAR), (nocert, FAR)):
        with_embedding(db, c, vec)
    return good, junior, nocert


def test_request_becomes_a_shortlist_waiting_for_review(env) -> None:
    db = env.db
    recruiters(db)
    good, junior, nocert = seed_candidates(db)
    req = hr.create_request(db, base_input())
    db.commit()
    assert req.reference.startswith("REQ-") and req.status == "new"
    env.drain()
    db.expire_all()

    req = db.get(Request, req.id)
    assert req.status == "ready_for_review" and req.owner_id is not None
    reqs = db.scalars(select(RequestRequirement).where(RequestRequirement.request_id == req.id)).all()
    assert {(r.kind, r.category) for r in reqs} >= {("must", "certification"), ("must", "years_experience"), ("nice", "skill")}

    items = db.scalars(select(ShortlistItem).where(ShortlistItem.request_id == req.id).order_by(ShortlistItem.rank)).all()
    # hard filters: CFA is required, so Cara (no CFA, not mentioned) is out; Bob is under 5 years
    assert [i.candidate_id for i in items] == [good.id]
    top = items[0]
    assert top.score > 60 and top.agent_rank == 1 and top.status == "suggested"
    statuses = {r["text"]: r["status"] for r in top.score_breakdown["requirements"]}
    assert statuses["CFA"] == "met" and statuses["5 years of experience"] == "met"
    assert top.explanation["source"] == "model" and top.explanation["prompt_version"] == "matcher-v1"
    # only quotes that exist in the CV survive
    assert [s["quote"] for s in top.explanation["strengths"]] == ["Accountant, Acme, 2018"]
    assert top.explanation["unverified_quotes_dropped"] == 1

    item = db.scalars(select(ReviewItem).where(ReviewItem.type == "shortlist_ready")).one()
    assert item.owner_id == req.owner_id
    actions = {a.action for a in db.scalars(select(AuditLog))}
    assert {"request.received", "agent.parse_request", "request.owner_assigned", "agent.matcher", "request.shortlist_ready"} <= actions


def test_ranking_is_code_and_prefers_stronger_candidates(env) -> None:
    db = env.db
    recruiters(db, 1)
    strong = add_candidate(db, "Strong One", certs=["CFA"], skills=["microsoft excel"], years=12, title="Senior Accountant",
                           text="Strong One. CFA. Excel. Accountant, Acme, 2018.")
    weak = add_candidate(db, "Weak One", certs=["CFA"], skills=[], years=5.5, title="Clerk", text="Weak One. CFA. Clerk, Acme, 2018.")
    with_embedding(db, strong, NEAR)
    with_embedding(db, weak, NEAR)
    hr.create_request(db, base_input())
    db.commit()
    env.drain()
    db.expire_all()
    items = db.scalars(select(ShortlistItem).order_by(ShortlistItem.rank)).all()
    assert [i.candidate_id for i in items] == [strong.id, weak.id]
    assert items[0].score > items[1].score


def test_owner_is_the_recruiter_with_the_fewest_open_requests(env) -> None:
    db = env.db
    a, b = recruiters(db, 2)
    seed_candidates(db)
    for _ in range(3):
        hr.create_request(db, base_input())
        db.commit()
        env.drain()
    db.expire_all()
    owners = [r.owner_id for r in db.scalars(select(Request).order_by(Request.id))]
    assert owners == [a.id, b.id, a.id]


def test_missing_job_title_and_description_means_needs_info_with_a_drafted_question(env) -> None:
    db = env.db
    recruiters(db)
    env.client.parsed = ParsedRequest()
    req = hr.create_request(db, base_input(job_title="", description="", must_text="", nice_text=""))
    db.commit()
    env.drain()
    db.expire_all()
    req = db.get(Request, req.id)
    assert req.status == "needs_info"
    item = db.scalars(select(ReviewItem).where(ReviewItem.type == "request_needs_info")).one()
    assert "the job title" in item.reason and "Hello Gina" in item.payload["draft_question"]
    assert db.scalar(select(func.count()).select_from(ShortlistItem)) == 0


def test_invented_requirements_are_dropped_but_company_lists_are_kept(env) -> None:
    db = env.db
    recruiters(db)
    env.client.parsed = ParsedRequest(
        job_title="Senior Accountant",
        must_have=[Req(text="PhD in astrophysics", category="education"), Req(text="CFA", category="certification")],
    )
    req = hr.create_request(db, base_input(must_text="CFA\nFluent Arabic", nice_text=""))
    db.commit()
    env.drain()
    texts = {r.text for r in db.scalars(select(RequestRequirement).where(RequestRequirement.request_id == req.id))}
    assert "PhD in astrophysics" not in texts  # not in the request: dropped
    assert "Fluent Arabic" in texts  # the company wrote it: kept even though the model missed it


def test_model_down_uses_fallback_then_recovers(env) -> None:
    db = env.db
    recruiters(db)
    good, *_ = seed_candidates(db)
    env.client.up = False
    req = hr.create_request(db, base_input())
    db.commit()
    env.drain()
    db.expire_all()
    req = db.get(Request, req.id)
    assert req.status == "ready_for_review" and req.data["ai_pending"] is True
    items = db.scalars(select(ShortlistItem)).all()
    assert items and all(i.explanation["source"] == "template" for i in items)
    assert items[0].score_breakdown["retrieval"] == "keyword_fallback"
    assert db.scalars(select(Job).where(Job.type == "request.reprocess")).one()

    env.client.up = True
    for job in db.scalars(select(Job).where(Job.status == "queued")):
        job.run_after = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    env.drain()
    db.expire_all()
    req = db.get(Request, req.id)
    assert req.data["ai_pending"] is False
    items = db.scalars(select(ShortlistItem)).all()
    assert all(i.explanation["source"] == "model" for i in items)
    assert items[0].score_breakdown["retrieval"] == "semantic"


def test_nationality_requirement_only_matches_cvs_that_state_it(env) -> None:
    db = env.db
    recruiters(db)
    has = add_candidate(db, "Has Nat", certs=["CFA"], years=9, nationality="Bahraini", title="Accountant", text="Has Nat CFA Accountant")
    hasnt = add_candidate(db, "No Nat", certs=["CFA"], years=9, title="Accountant", text="No Nat CFA Accountant")
    with_embedding(db, has, NEAR)
    with_embedding(db, hasnt, NEAR)
    req = hr.create_request(db, base_input(nationality="Bahraini", nationality_justification="Government contract requires it"))
    db.commit()
    env.drain()
    db.expire_all()
    assert [i.candidate_id for i in db.scalars(select(ShortlistItem))] == [has.id]
    assert db.get(Request, req.id).data["match_summary"]["excluded_unknown_nationality"] == 1


def test_rematch_keeps_removed_and_manual_items(env) -> None:
    db = env.db
    recruiters(db)
    good, junior, nocert = seed_candidates(db)
    other = add_candidate(db, "Dan Also", certs=["CFA"], years=8, title="Accountant", text="Dan Also CFA Accountant, Delta, 2019")
    with_embedding(db, other, NEAR)
    req = hr.create_request(db, base_input())
    db.commit()
    env.drain()
    db.expire_all()
    items = {i.candidate_id: i for i in db.scalars(select(ShortlistItem))}
    items[other.id].status = "removed"
    db.add(ShortlistItem(request_id=req.id, candidate_id=nocert.id, rank=9, score=0, explanation={"manual": True}, status="suggested"))
    db.commit()

    queue.enqueue(db, "request.rematch", {"request_id": req.id})
    db.commit()
    env.drain()
    db.expire_all()
    final = {i.candidate_id: i for i in db.scalars(select(ShortlistItem))}
    assert final[other.id].status == "removed"
    assert nocert.id in final and final[nocert.id].explanation.get("manual")
    assert good.id in final


def test_scoring_requirement_checks_directly() -> None:
    v = CandidateVersion(
        full_name="X", certifications=["CFA"], skills=["python"], years_experience=6.0, titles=["risk analyst"],
        industries=["banking"], education_text="BSc Finance University of X", qualifications=[],
        search_text="Worked in banking. Fluent in Arabic and English.",
        data={"industry_years": {"banking": 4.0}},
    )
    def r(cat, text, kind="must"):
        return evaluate(Requirement(None, kind, cat, text), v).status

    assert r("certification", "Chartered Financial Analyst") == "met"
    assert r("certification", "CPA") == "unmet"
    assert r("years_experience", "5 years of experience") == "met"
    assert r("years_experience", "8 years of experience") == "unmet"
    assert r("years_experience", "5 years in banking") == "unmet"  # only 4 in banking
    assert r("years_experience", "3 years in banking") == "met"
    assert r("job_title", "Risk Analyst") == "met" and r("job_title", "Chef") == "unmet"
    assert r("skill", "Python") == "met" and r("skill", "Rust") == "unmet"
    assert r("language", "Arabic and English") == "met" and r("language", "French") == "unmet"
    assert r("education", "Bachelor degree in finance") == "met"
    assert r("nationality", "Bahraini") == "unknown"  # never guessed


def test_score_uses_only_the_components_that_apply() -> None:
    v = CandidateVersion(full_name="X", certifications=["CFA"], skills=[], years_experience=10.0, titles=[], search_text="CFA")
    s = score_candidate([Requirement(1, "must", "certification", "CFA")], v, job_title=None, min_years=None, similarity=None)
    assert s.score == 100.0 and s.components["similarity"] is None and s.components["title"] is None
    s2 = score_candidate([Requirement(1, "must", "certification", "CPA")], v, job_title=None, min_years=None, similarity=None)
    assert s2.score == 0.0


def test_candidates_without_an_embedding_yet_are_still_considered(env) -> None:
    """While embedding jobs are queued behind a big ingest, matching must not silently ignore those people."""
    db = env.db
    embedded = add_candidate(db, "Has Vector", certs=["CFA"], years=9, title="Accountant", text="Has Vector CFA accountant banking")
    waiting = add_candidate(db, "No Vector Yet", certs=["CFA"], years=9, title="Accountant", text="No Vector Yet CFA accountant banking")
    with_embedding(db, embedded, NEAR)
    result = retrieval.retrieve(db, "CFA accountant banking", [embedded.id, waiting.id])
    assert {v.candidate_id for v in result.versions} == {embedded.id, waiting.id}
    assert any("not been embedded yet" in n for n in result.notes)
    assert result.mode == "semantic"


def test_no_note_when_everyone_is_embedded(env) -> None:
    db = env.db
    a = add_candidate(db, "Has Vector", certs=["CFA"], years=9, title="Accountant", text="CFA accountant")
    with_embedding(db, a, NEAR)
    result = retrieval.retrieve(db, "CFA accountant", [a.id])
    assert result.notes == []
