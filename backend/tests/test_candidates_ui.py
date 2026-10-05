import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.config import settings
from app.core.models import (
    AuditLog,
    Candidate,
    CandidateEmbedding,
    CandidateIdentifier,
    CandidateVersion,
    File,
    Job,
    ReviewItem,
)
from app.llm.ollama import EmbedResult, LLMUnavailable
from app.matching import search as search_mod
from tests import factories
from tests.test_auth_shell import login


def add_candidate(db, name, *, text="", skills=(), certs=(), years=5.0, title="Accountant", industries=(),
                  source="cv_folder", nationality=None, education="BSc Accounting", emails=(), days_ago=0):
    cand = Candidate(source=source)
    db.add(cand)
    db.flush()
    ver = CandidateVersion(
        candidate_id=cand.id, version_no=1, full_name=name, search_text=text or f"{name} {title}",
        skills=list(skills), certifications=list(certs), years_experience=years, current_title=title,
        titles=[title.lower()], industries=list(industries), nationality=nationality, education_text=education,
        applied_at=datetime.now(timezone.utc) - timedelta(days=days_ago), extraction_status="ok",
        data={"name": name, "emails": list(emails), "skills": list(skills), "certifications": list(certs),
              "experience": [{"title": title, "company": "Acme", "start": "2018-01-01", "end": None,
                              "is_current": True, "industry": "banking", "snippet": "Accountant, Acme, 2018"}],
              "education": [{"institution": "Uni", "degree": "BSc", "field": "Accounting"}]},
    )
    db.add(ver)
    db.flush()
    cand.current_version_id = ver.id
    for e in emails:
        db.add(CandidateIdentifier(candidate_id=cand.id, candidate_version_id=ver.id, kind="email", value=e))
    db.commit()
    return cand


@pytest.fixture()
def no_ai(monkeypatch):
    class Down:
        def embed(self, *a, **k):
            raise LLMUnavailable("down")

    monkeypatch.setattr(search_mod, "get_client", lambda: Down())


@pytest.fixture()
def as_recruiter(client, users):
    login(client, "recruiter@test.local")
    return client


def names_in(html: str) -> list[str]:
    return re.findall(r'<td>\s*<a href="/candidates/\d+"><strong>([^<]+)</strong>', html)


def test_list_requires_login(client) -> None:
    assert client.get("/candidates", follow_redirects=False).status_code == 303


def test_empty_state_explains_what_will_appear(as_recruiter, no_ai) -> None:
    html = as_recruiter.get("/candidates").text
    assert "No candidates yet" in html and "<strong>0</strong> candidates" in html


def test_filters_by_cert_skill_years_industry_title_name(db, as_recruiter, no_ai) -> None:
    add_candidate(db, "Alice Cfa", certs=["CFA"], skills=["python", "microsoft excel"], years=9, industries=["banking"], title="Risk Analyst")
    add_candidate(db, "Bob Plain", certs=[], skills=["microsoft excel"], years=2, industries=["retail"], title="Cashier")
    add_candidate(db, "Cara Pmp", certs=["PMP"], skills=["python"], years=12, industries=["banking"], title="Project Manager")

    def q(**params):
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return as_recruiter.get(f"/candidates?{qs}").text

    assert names_in(q(certs="CFA")) == ["Alice Cfa"]
    assert names_in(q(certs="chartered financial analyst")) == ["Alice Cfa"]  # canonicalised
    assert sorted(names_in(q(skills="python"))) == ["Alice Cfa", "Cara Pmp"]
    assert names_in(q(skills="python,excel")) == ["Alice Cfa"]  # all skills must match
    assert sorted(names_in(q(min_years=5))) == ["Alice Cfa", "Cara Pmp"]
    assert names_in(q(min_years=5, max_years=10)) == ["Alice Cfa"]
    assert sorted(names_in(q(industry="banking"))) == ["Alice Cfa", "Cara Pmp"]
    assert names_in(q(title="cashier")) == ["Bob Plain"]
    assert names_in(q(name="bob")) == ["Bob Plain"]
    assert names_in(q(education="accounting")) != []
    assert names_in(q(certs="CFA", industry="retail")) == []


def test_count_line_names_the_filter(db, as_recruiter, no_ai) -> None:
    for i in range(3):
        add_candidate(db, f"Person {i}", certs=["CFA"])
    html = as_recruiter.get("/candidates?certs=CFA").text
    assert re.search(r"<strong>3</strong> candidates with CFA", html)


def test_nationality_filter_only_shows_when_stated_and_only_matches_stated(db, as_recruiter, no_ai) -> None:
    add_candidate(db, "Has Nat", nationality="Indian")
    add_candidate(db, "No Nat")
    html = as_recruiter.get("/candidates?nationality=indian").text
    assert names_in(html) == ["Has Nat"]
    assert 'name="nationality"' in html


def test_nationality_filter_hidden_when_no_cv_states_it(db, as_recruiter, no_ai) -> None:
    add_candidate(db, "No Nat")
    assert 'name="nationality"' not in as_recruiter.get("/candidates").text


def test_free_text_search_finds_words_inside_the_cv_with_highlight(db, as_recruiter, no_ai) -> None:
    add_candidate(db, "Dina One", text="Dina One. Managed audits for a manufacturing plant in Riyadh for six years.")
    add_candidate(db, "Evan Two", text="Evan Two. Barista and shop assistant.")
    html = as_recruiter.get("/candidates?q=manufacturing").text
    assert names_in(html) == ["Dina One"]
    assert "<mark>manufacturing</mark>" in html
    assert "Meaning-based search is paused" in html  # AI down is explained, keyword search still works


def test_highlight_is_html_escaped(db, as_recruiter, no_ai) -> None:
    add_candidate(db, "Xss Test", text="Worked on <script>alert(1)</script> manufacturing systems")
    html = as_recruiter.get("/candidates?q=manufacturing").text
    assert "<script>alert(1)</script>" not in html
    assert "<mark>manufacturing</mark>" in html


def test_semantic_search_finds_meaning_without_the_keyword(db, as_recruiter, monkeypatch) -> None:
    a = add_candidate(db, "Near Match", text="Chartered accountant, audits and reconciliations")
    b = add_candidate(db, "Far Match", text="Chef and baker")
    dim = settings.embedding_dim
    near, far = [1.0] + [0.0] * (dim - 1), [0.0] * (dim - 1) + [1.0]
    for cand, vec in ((a, near), (b, far)):
        db.add(CandidateEmbedding(candidate_version_id=cand.current_version_id, kind="profile", embedding=vec, model="m"))
    db.commit()

    class Fake:
        def embed(self, texts, kind, model=None):
            return EmbedResult([near for _ in texts], "m", 1)

    monkeypatch.setattr(search_mod, "get_client", lambda: Fake())
    html = as_recruiter.get("/candidates?q=bookkeeper").text  # no CV contains this word
    assert names_in(html) == ["Near Match"]
    assert "paused" not in html


def test_htmx_request_returns_only_the_results_fragment(db, as_recruiter, no_ai) -> None:
    add_candidate(db, "Frag Test")
    html = as_recruiter.get("/candidates?name=frag", headers={"HX-Request": "true"}).text
    assert "<html" not in html and "Frag Test" in html


def test_pagination(db, as_recruiter, no_ai) -> None:
    for i in range(30):
        add_candidate(db, f"Person {i:02d}", days_ago=i)
    p1 = as_recruiter.get("/candidates").text
    assert "Page 1 of 2" in p1 and len(names_in(p1)) == 25
    assert len(names_in(as_recruiter.get("/candidates?page=2").text)) == 5


def test_merged_candidates_are_not_listed(db, as_recruiter, no_ai) -> None:
    keep = add_candidate(db, "Keep Me")
    gone = add_candidate(db, "Merged Away")
    gone.merged_into_id = keep.id
    db.commit()
    assert names_in(as_recruiter.get("/candidates").text) == ["Keep Me"]
    assert as_recruiter.get(f"/candidates/{gone.id}", follow_redirects=False).headers["location"] == f"/candidates/{keep.id}"


def test_profile_tabs_render_with_source_snippets(db, as_recruiter) -> None:
    cand = add_candidate(db, "Pro File", emails=["pro@example.com"], skills=["python"], certs=["CFA"])
    for tab, needle in (("overview", "pro@example.com"), ("experience", "Accountant, Acme, 2018"),
                        ("education", "Accounting"), ("versions", "Version 1"), ("applications", "No applications yet"),
                        ("activity", "No activity recorded yet")):
        resp = as_recruiter.get(f"/candidates/{cand.id}?tab={tab}")
        assert resp.status_code == 200 and needle in resp.text, tab
    assert as_recruiter.get("/candidates/99999").status_code == 404
    assert as_recruiter.get(f"/candidates/{cand.id}?tab=nonsense").status_code == 200


def test_profile_escapes_stored_html(db, as_recruiter) -> None:
    cand = add_candidate(db, "<img src=x onerror=alert(1)>")
    assert "<img src=x" not in as_recruiter.get(f"/candidates/{cand.id}").text


def test_original_file_is_downloaded_as_attachment_only(db, as_recruiter, tmp_path) -> None:
    path = factories.write_docx(tmp_path / "orig.docx")
    f = File(sha256="a" * 64, original_name="orig.docx", stored_path=str(path), size_bytes=10, origin="upload")
    db.add(f)
    db.commit()
    resp = as_recruiter.get(f"/files/{f.id}/original")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert db.scalars(select(AuditLog).where(AuditLog.action == "file.downloaded")).one()
    assert as_recruiter.get("/files/424242/original").status_code == 404


def csrf(client) -> str:
    return re.search(r'name="csrf-token" content="([^"]+)"', client.get("/candidates").text).group(1)


def test_upload_requires_csrf_and_valid_file(db, as_recruiter, tmp_path, monkeypatch, no_ai) -> None:
    monkeypatch.setattr(settings, "inbox_dir", tmp_path)
    docx = factories.write_docx(tmp_path / "src" / "cv.docx") if (tmp_path / "src").mkdir() is None else None
    data = docx.read_bytes()
    assert as_recruiter.post("/candidates/upload", files={"cv": ("cv.docx", data)}).status_code == 403

    token = csrf(as_recruiter)
    bad = as_recruiter.post("/candidates/upload", files={"cv": ("evil.pdf", b"not a pdf at all")},
                            headers={"X-CSRF-Token": token})
    assert bad.status_code == 400 and "Unsupported file type" in bad.text
    assert db.scalar(select(File.id)) is None

    ok = as_recruiter.post("/candidates/upload", files={"cv": ("../../my cv.docx", data)},
                           headers={"X-CSRF-Token": token}, follow_redirects=False)
    assert ok.status_code == 303 and ok.headers["location"] == "/candidates?uploaded=1"
    f = db.scalars(select(File)).one()
    assert f.origin == "upload" and "/" not in f.original_name and ".." not in f.original_name
    assert db.scalars(select(Job).where(Job.type == "cv.process")).one()
    assert db.scalars(select(AuditLog).where(AuditLog.action == "file.uploaded")).one()
    assert not list(tmp_path.glob(".upload-*"))  # scratch file cleaned up

    again = as_recruiter.post("/candidates/upload", files={"cv": ("again.docx", data)}, headers={"X-CSRF-Token": token})
    assert again.status_code == 409 and "already in the database" in again.text


def test_oversize_upload_is_rejected(as_recruiter, tmp_path, monkeypatch, no_ai) -> None:
    monkeypatch.setattr(settings, "inbox_dir", tmp_path)
    monkeypatch.setattr(settings, "max_upload_mb", 0)
    resp = as_recruiter.post("/candidates/upload", files={"cv": ("big.pdf", b"x" * 100)},
                             headers={"X-CSRF-Token": csrf(as_recruiter)})
    assert resp.status_code == 400 and "larger than" in resp.text


def form(**kw):
    base = {"name": "Nadia Sourced", "email": "nadia@example.com", "phone": "", "location": "Dubai",
            "source": "LinkedIn", "source_detail": "https://linkedin.com/in/nadia"}
    base.update(kw)
    return base


def test_add_candidate_creates_profile_with_source(db, as_recruiter, no_ai) -> None:
    resp = as_recruiter.post("/candidates/new", data=form(), headers={"X-CSRF-Token": csrf(as_recruiter)},
                             follow_redirects=False)
    assert resp.status_code == 303
    cand = db.scalars(select(Candidate)).one()
    assert cand.source == "LinkedIn" and "linkedin.com/in/nadia" in cand.source_detail
    ver = db.get(CandidateVersion, cand.current_version_id)
    assert ver.full_name == "Nadia Sourced" and ver.data["emails"] == ["nadia@example.com"]
    assert db.scalars(select(AuditLog).where(AuditLog.action == "candidate.added_manually")).one().actor_name == "recruiter@test.local"
    assert db.scalars(select(Job).where(Job.type == "cv.embed")).one()
    page = as_recruiter.get(resp.headers["location"]).text
    assert "Source: LinkedIn" in page and "Found via" in page


def test_add_candidate_warns_before_duplicating(db, as_recruiter, no_ai) -> None:
    existing = add_candidate(db, "Nadia Sourced", emails=["nadia@example.com"])
    token = csrf(as_recruiter)
    warn = as_recruiter.post("/candidates/new", data=form(), headers={"X-CSRF-Token": token})
    assert warn.status_code == 200 and "probably already in the database" in warn.text
    assert "Same email" in warn.text and f"/candidates/{existing.id}" in warn.text
    assert db.scalar(select(Candidate.id).where(Candidate.id != existing.id)) is None  # nothing saved yet

    confirmed = as_recruiter.post("/candidates/new", data={**form(), "confirm": "1"}, headers={"X-CSRF-Token": token},
                                  follow_redirects=False)
    assert confirmed.status_code == 303 and confirmed.headers["location"] == f"/candidates/{existing.id}"
    db.expire_all()
    assert len(db.scalars(select(CandidateVersion).where(CandidateVersion.candidate_id == existing.id)).all()) == 2


def test_add_candidate_name_match_creates_review_item_not_a_merge(db, as_recruiter, no_ai) -> None:
    add_candidate(db, "Nadia Sourced", emails=["old@example.com"])
    token = csrf(as_recruiter)
    warn = as_recruiter.post("/candidates/new", data=form(email="new@example.com"), headers={"X-CSRF-Token": token})
    assert "Possible match found" in warn.text
    as_recruiter.post("/candidates/new", data={**form(email="new@example.com"), "confirm": "1"},
                      headers={"X-CSRF-Token": token})
    assert len(db.scalars(select(Candidate)).all()) == 2
    assert db.scalars(select(ReviewItem).where(ReviewItem.type == "possible_duplicate")).one()


def test_add_candidate_validation_and_csrf(as_recruiter, no_ai) -> None:
    assert as_recruiter.post("/candidates/new", data=form()).status_code == 403
    token = csrf(as_recruiter)
    assert "enter the candidate" in as_recruiter.post("/candidates/new", data=form(name=""), headers={"X-CSRF-Token": token}).text
    assert "Choose where" in as_recruiter.post("/candidates/new", data=form(source="Dark web"), headers={"X-CSRF-Token": token}).text


def test_add_candidate_with_cv_sends_form_details_with_the_file(db, as_recruiter, tmp_path, monkeypatch, no_ai) -> None:
    monkeypatch.setattr(settings, "inbox_dir", tmp_path)
    src = tmp_path / "s"
    src.mkdir()
    data = factories.write_docx(src / "c.docx").read_bytes()
    resp = as_recruiter.post("/candidates/new", data=form(), files={"cv": ("c.docx", data)},
                             headers={"X-CSRF-Token": csrf(as_recruiter)}, follow_redirects=False)
    assert resp.status_code == 303
    job = db.scalars(select(Job).where(Job.type == "cv.process")).one()
    assert job.payload["hints"]["source"] == "LinkedIn" and job.payload["hints"]["emails"] == ["nadia@example.com"]
    assert job.payload["created_by"]
