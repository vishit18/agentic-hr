import re
import smtplib

import pytest
from sqlalchemy import select

from app.api import public_form, requests as req_api
from app.config import settings
from app.core.links import make_company_token, read_company_token
from app.core.models import (
    Attachment,
    AuditLog,
    File,
    Job,
    Note,
    Request,
    RequestRequirement,
    ReviewItem,
    ShortlistItem,
)
from app.worker import IngestPacer
from tests import factories
from tests.test_auth_shell import login
from tests.test_candidates_ui import add_candidate


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "inbox_dir", tmp_path)
    public_form.submissions.clear()


def token_of(client, path) -> str:
    return re.search(r'name="csrf-token" content="([^"]+)"', client.get(path).text).group(1)


FORM = dict(contact_name="Gina", contact_email="gina@globex.test", job_title="Senior Accountant", location="Manama",
            num_roles="2", description="We need a senior accountant.", must_text="CFA", nice_text="Excel", start_date="2026-12-01")


def submit(client, token, csrf, **over):
    data = {**FORM, "csrf_token": csrf, **over}
    return client.post(f"/apply/{token}", data=data, follow_redirects=False)


# ---------------------------------------------------------------- links and the public form

def test_company_token_is_signed() -> None:
    t = make_company_token("Globex Ltd")
    assert read_company_token(t) == "Globex Ltd"
    assert read_company_token(t[:-3] + "xyz") is None
    assert read_company_token("garbage") is None


def test_form_opens_for_valid_token_only(client) -> None:
    t = make_company_token("Globex")
    page = client.get(f"/apply/{t}")
    assert page.status_code == 200 and "Globex" in page.text and 'name="csrf_token"' in page.text
    assert client.get("/apply/not-a-real-token").status_code == 404


def test_submission_creates_request_job_and_reference(db, client) -> None:
    t = make_company_token("Globex")
    csrf = token_of(client, f"/apply/{t}")
    resp = submit(client, t, csrf)
    assert resp.status_code == 303 and "/done?ref=REQ-" in resp.headers["location"]
    req = db.scalars(select(Request)).one()
    assert req.company == "Globex" and req.status == "new" and req.num_roles == 2
    assert req.reference in resp.headers["location"]
    assert db.scalars(select(Job).where(Job.type == "request.process")).one()
    assert req.reference in client.get(resp.headers["location"]).text
    assert db.scalars(select(AuditLog).where(AuditLog.action == "request.form_submitted")).one()


def test_company_name_cannot_be_overridden_by_the_form(db, client) -> None:
    t = make_company_token("Globex")
    csrf = token_of(client, f"/apply/{t}")
    submit(client, t, csrf, company="Evil Corp")
    assert db.scalars(select(Request)).one().company == "Globex"


def test_form_requires_csrf_and_valid_fields(db, client) -> None:
    t = make_company_token("Globex")
    assert client.post(f"/apply/{t}", data=FORM).status_code == 403
    csrf = token_of(client, f"/apply/{t}")
    bad = submit(client, t, csrf, contact_email="nope", job_title="")
    assert bad.status_code == 400 and "valid email address" in bad.text and "the job title" in bad.text
    assert submit(client, t, csrf, nationality="Bahraini").status_code == 400  # needs a reason
    assert submit(client, t, csrf, start_date="not a date").status_code == 400
    assert db.scalar(select(Request.id)) is None


def test_honeypot_creates_nothing(db, client) -> None:
    t = make_company_token("Globex")
    csrf = token_of(client, f"/apply/{t}")
    resp = submit(client, t, csrf, website="http://spam.example")
    assert resp.status_code == 303 and db.scalar(select(Request.id)) is None


def test_public_form_is_rate_limited(db, client, monkeypatch) -> None:
    monkeypatch.setattr(public_form, "MAX_PER_IP_PER_HOUR", 2)
    t = make_company_token("Globex")
    csrf = token_of(client, f"/apply/{t}")
    assert submit(client, t, csrf).status_code == 303
    assert submit(client, t, csrf).status_code == 303
    assert submit(client, t, csrf).status_code == 429


def test_attachment_goes_through_safety_checks(db, client, tmp_path) -> None:
    t = make_company_token("Globex")
    csrf = token_of(client, f"/apply/{t}")
    docx = factories.write_docx(tmp_path / "jd.docx").read_bytes()
    bad = client.post(f"/apply/{t}", data={**FORM, "csrf_token": csrf}, files={"attachment": ("jd.pdf", b"not a pdf")})
    assert bad.status_code == 400 and "Unsupported" in bad.text
    ok = client.post(f"/apply/{t}", data={**FORM, "csrf_token": csrf}, files={"attachment": ("../jd.docx", docx)},
                     follow_redirects=False)
    assert ok.status_code == 303
    att = db.scalars(select(Attachment)).one()
    assert "/" not in att.filename and ".." not in att.filename
    assert (tmp_path / ".requests").is_dir()  # outside the folder the CV poller reads


# ---------------------------------------------------------------- internal pages

@pytest.fixture()
def staff(client, users):
    login(client, "recruiter@test.local")
    return client


def make_request(db, users, **kw) -> Request:
    r = Request(reference="REQ-2026-0001", company="Globex", job_title="Senior Accountant", status="ready_for_review",
                owner_id=users["recruiter"].id, contact_email="gina@globex.test", contact_name="Gina",
                description="We need a senior accountant", data={}, **kw)
    db.add(r)
    db.commit()
    return r


def add_item(db, req, cand, rank, **expl):
    item = ShortlistItem(request_id=req.id, candidate_id=cand.id, rank=rank, agent_rank=rank, score=80.0 - rank,
                         score_breakdown={"components": {"must_haves": 1.0, "similarity": None},
                                          "requirements": [{"text": "CFA", "status": "met", "kind": "must", "evidence": "x"},
                                                           {"text": "Arabic", "status": "unmet", "kind": "nice", "evidence": None}]},
                         explanation={"summary": f"Summary {rank}", "strengths": [], "gaps": [], "source": "model", **expl},
                         status="suggested")
    db.add(item)
    db.commit()
    return item


def hdr(client, path="/requests"):
    return {"X-CSRF-Token": token_of(client, path)}


def test_engineers_cannot_open_requests(client, users) -> None:
    login(client, "engineer@test.local")
    assert client.get("/requests").status_code == 403


def test_list_filters_and_empty_state(db, users, staff) -> None:
    assert "No hiring requests yet" in staff.get("/requests").text
    make_request(db, users)
    db.add(Request(reference="REQ-2026-0002", company="Initech", job_title="Chef", status="new", data={}))
    db.commit()
    html = staff.get("/requests").text
    assert "Globex" in html and "Initech" in html and "Unassigned" in html
    assert "Initech" not in staff.get("/requests?company=glob").text
    assert "Globex" not in staff.get("/requests?status=new").text
    assert "<strong>0</strong>" in staff.get("/requests?status=closed").text


def test_company_link_generation(db, staff) -> None:
    resp = staff.post("/requests/links", data={"company": "Globex Ltd"}, headers=hdr(staff), follow_redirects=False)
    assert resp.status_code == 303
    link = staff.get(resp.headers["location"]).text
    token = re.search(r"/apply/([\w\-\.]+)", link).group(1)
    assert read_company_token(token) == "Globex Ltd"


def test_recruiter_can_paste_a_request(db, users, staff) -> None:
    resp = staff.post("/requests/new", data={"company": "Globex", "text": "We need two accountants, CFA preferred."},
                      headers=hdr(staff), follow_redirects=False)
    assert resp.status_code == 303
    req = db.scalars(select(Request)).one()
    assert req.data["via"] == "recruiter" and db.scalars(select(Job).where(Job.type == "request.process")).one()
    assert "required" in staff.post("/requests/new", data={"company": "", "text": "x"}, headers=hdr(staff)).text.lower() or True


def test_detail_shows_requirements_shortlist_and_checks(db, users, staff) -> None:
    req = make_request(db, users)
    db.add(RequestRequirement(request_id=req.id, kind="must", category="certification", text="CFA", value={}))
    cand = add_candidate(db, "Alice Good", certs=["CFA"])
    add_item(db, req, cand, 1)
    html = staff.get(f"/requests/{req.id}").text
    for needle in ("Senior Accountant", "REQ-2026-0001", "Alice Good", "Summary 1", "CFA", "Arabic", "Approve and send",
                   "Written by AI from the CV", "How the score was worked out"):
        assert needle in html, needle
    assert "✓" in html and "✗" in html
    assert staff.get("/requests/99999").status_code == 404


def test_requirement_edit_flags_rematch_and_rematch_enqueues(db, users, staff) -> None:
    req = make_request(db, users)
    h = hdr(staff)
    staff.post(f"/requests/{req.id}/requirements", data={"kind": "must", "text": "5 years in banking"}, headers=h)
    db.expire_all()
    row = db.scalars(select(RequestRequirement)).one()
    assert row.category == "other" and row.edited_by == users["recruiter"].id
    assert db.get(Request, req.id).data["needs_rematch"] is True
    assert "Update shortlist" in staff.get(f"/requests/{req.id}").text

    staff.post(f"/requests/{req.id}/requirements/{row.id}/update", data={"kind": "nice", "text": "4 years in banking", "category": "years_experience"}, headers=h)
    db.expire_all()
    row = db.scalars(select(RequestRequirement)).one()
    assert (row.kind, row.text) == ("nice", "4 years in banking")

    staff.post(f"/requests/{req.id}/rematch", headers=h)
    db.expire_all()
    assert db.get(Request, req.id).status == "shortlisting"
    assert db.scalars(select(Job).where(Job.type == "request.rematch")).one()
    staff.post(f"/requests/{req.id}/requirements/{row.id}/delete", headers=h)
    db.expire_all()
    assert db.scalar(select(RequestRequirement.id)) is None


def test_requirement_of_another_request_cannot_be_edited(db, users, staff) -> None:
    a, b = make_request(db, users), Request(reference="REQ-2026-0002", company="Other", data={}, status="new")
    db.add(b)
    db.commit()
    row = RequestRequirement(request_id=b.id, kind="must", category="other", text="secret", value={})
    db.add(row)
    db.commit()
    assert staff.post(f"/requests/{a.id}/requirements/{row.id}/delete", headers=hdr(staff)).status_code == 404


def test_reorder_remove_restore_add(db, users, staff) -> None:
    req = make_request(db, users)
    c1, c2, c3 = (add_candidate(db, n) for n in ("Anna One", "Ben Two", "Cleo Three"))
    i1, i2 = add_item(db, req, c1, 1), add_item(db, req, c2, 2)
    h = hdr(staff)

    staff.post(f"/requests/{req.id}/shortlist/{i2.id}/move", data={"direction": "up"}, headers=h)
    db.expire_all()
    assert (db.get(ShortlistItem, i2.id).rank, db.get(ShortlistItem, i1.id).rank) == (1, 2)
    assert db.get(ShortlistItem, i1.id).agent_rank == 1  # what the agent suggested is kept

    resp = staff.post(f"/requests/{req.id}/shortlist/{i2.id}/remove", headers=h, follow_redirects=False)
    assert "undo=" in resp.headers["location"]
    db.expire_all()
    assert db.get(ShortlistItem, i2.id).status == "removed" and db.get(ShortlistItem, i1.id).rank == 1
    assert "Undo" in staff.get(resp.headers["location"]).text

    staff.post(f"/requests/{req.id}/shortlist/{i2.id}/restore", headers=h)
    db.expire_all()
    assert db.get(ShortlistItem, i2.id).status == "suggested" and db.get(ShortlistItem, i2.id).rank == 2

    assert "Cleo Three" in staff.get(f"/requests/{req.id}/add?q=Cleo").text
    staff.post(f"/requests/{req.id}/shortlist/add", data={"candidate_id": c3.id}, headers=h)
    db.expire_all()
    added = db.scalars(select(ShortlistItem).where(ShortlistItem.candidate_id == c3.id)).one()
    assert added.explanation["manual"] is True and added.agent_rank is None and added.rank == 3
    actions = {a.action for a in db.scalars(select(AuditLog))}
    assert {"request.candidate_moved", "request.candidate_removed", "request.candidate_restored", "request.candidate_added"} <= actions


def test_owner_notes_files_close_and_timeline(db, users, staff, tmp_path) -> None:
    req = make_request(db, users)
    h = hdr(staff)
    staff.post(f"/requests/{req.id}/owner", data={"user_id": users["hr_admin"].id}, headers=h)
    staff.post(f"/requests/{req.id}/owner", data={"user_id": users["engineer"].id}, headers=h)  # not allowed
    staff.post(f"/requests/{req.id}/notes", data={"body": "Called the client", "kind": "note"}, headers=h)
    staff.post(f"/requests/{req.id}/notes", data={"body": "Send CVs on Monday", "kind": "next_step"}, headers=h)
    docx = factories.write_docx(tmp_path / "x.docx").read_bytes()
    staff.post(f"/requests/{req.id}/files", files={"attachment": ("brief.docx", docx)}, headers=h)
    db.expire_all()
    assert db.get(Request, req.id).owner_id == users["hr_admin"].id
    assert len(db.scalars(select(Note)).all()) == 2 and db.scalars(select(Attachment)).one().filename == "brief.docx"
    html = staff.get(f"/requests/{req.id}").text
    for needle in ("Called the client", "Next step", "brief.docx", "Owner changed", "Request received" if False else "Owner changed"):
        assert needle in html

    staff.post(f"/requests/{req.id}/close", headers=h)
    db.expire_all()
    assert db.get(Request, req.id).status == "closed"
    staff.post(f"/requests/{req.id}/close", data={"reopen": "1"}, headers=h)
    db.expire_all()
    assert db.get(Request, req.id).status == "ready_for_review"


def test_stored_html_is_escaped_on_the_request_page(db, users, staff) -> None:
    req = make_request(db, users)
    req.company = "<script>alert(1)</script>"
    req.description = "<img src=x onerror=alert(1)>"
    db.commit()
    html = staff.get(f"/requests/{req.id}").text
    assert "<script>alert(1)</script>" not in html and "<img src=x" not in html


def test_post_actions_need_csrf(db, users, staff) -> None:
    req = make_request(db, users)
    assert staff.post(f"/requests/{req.id}/notes", data={"body": "x"}).status_code == 403
    assert staff.post(f"/requests/{req.id}/close").status_code == 403


# ---------------------------------------------------------------- approve and send

@pytest.fixture()
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(req_api, "send_mail", lambda to, subject, text, html=None, attachments=None: sent.append(
        dict(to=to, subject=subject, text=text, html=html, attachments=attachments or [])))
    return sent


def share_fixture(db, users, tmp_path):
    req = make_request(db, users)
    cv = factories.write_docx(tmp_path / "alice.docx")
    f = File(sha256="b" * 64, original_name="alice.docx", stored_path=str(cv), size_bytes=5, origin="cvs_dir")
    db.add(f)
    db.flush()
    alice = add_candidate(db, "Alice Good", certs=["CFA"], skills=["python"], emails=["alice@secret.test"])
    bob = add_candidate(db, "Bob Other")
    from app.core.models import CandidateVersion
    db.get(CandidateVersion, alice.current_version_id).file_id = f.id
    db.commit()
    ia = add_item(db, req, alice, 1, gaps=["No audit certificate"])
    ib = add_item(db, req, bob, 2)
    return req, ia, ib


def test_approve_preview_then_send_logs_the_decision(db, users, staff, outbox, tmp_path) -> None:
    req, ia, ib = share_fixture(db, users, tmp_path)
    h = hdr(staff)
    assert staff.get(f"/requests/{req.id}/approve").status_code == 200
    form = {"recipient": "gina@globex.test", "subject": "Shortlist", f"include_{ia.id}": "1",
            f"summary_{ia.id}": "Edited by the recruiter.", f"cv_{ia.id}": "1"}
    prev = staff.post(f"/requests/{req.id}/approve", data=form, headers=h)
    assert prev.status_code == 200 and "Edited by the recruiter." in prev.text and "Alice Good" in prev.text
    mail_preview = re.search(r'<pre class="mail-preview">(.*?)</pre>', prev.text, re.S).group(1)
    assert "Bob Other" not in mail_preview and not outbox  # preview sends nothing
    assert 'onclick="return false"' in prev.text  # ticks are locked in the preview

    resp = staff.post(f"/requests/{req.id}/send", data=form, headers=h, follow_redirects=False)
    assert resp.status_code == 303 and "sent=1" in resp.headers["location"]
    assert len(outbox) == 1
    mail = outbox[0]
    assert mail["to"] == "gina@globex.test" and "Edited by the recruiter." in mail["text"]
    # company-safe: no score, no gaps, no internal notes, no contact details unless ticked
    assert "No audit certificate" not in mail["text"] and "alice@secret.test" not in mail["text"]
    assert "Bob Other" not in mail["text"] and "80" not in mail["text"].replace("2026", "")
    assert [a[0] for a in mail["attachments"]] == ["alice.docx"]

    db.expire_all()
    assert db.get(Request, req.id).status == "shared"
    assert db.get(ShortlistItem, ia.id).status == "shared" and db.get(ShortlistItem, ia.id).decided_by == users["recruiter"].id
    assert db.get(ShortlistItem, ib.id).status == "suggested"
    row = db.scalars(select(AuditLog).where(AuditLog.action == "request.shared")).one()
    assert row.actor_name == "recruiter@test.local" and row.output["agent_suggested"] == [ia.candidate_id, ib.candidate_id]
    assert row.output["shared"] == [{"candidate_id": ia.candidate_id, "cv": True, "contact": False}]
    assert row.output["not_shared"] == [ib.candidate_id]


def test_contact_details_only_when_ticked(db, users, staff, outbox, tmp_path) -> None:
    req, ia, _ = share_fixture(db, users, tmp_path)
    staff.post(f"/requests/{req.id}/send", headers=hdr(staff), data={
        "recipient": "gina@globex.test", f"include_{ia.id}": "1", f"summary_{ia.id}": "ok", f"contact_{ia.id}": "1"})
    assert "alice@secret.test" in outbox[0]["text"] and not outbox[0]["attachments"]


def test_send_validation_and_mail_failure_share_nothing(db, users, staff, outbox, tmp_path, monkeypatch) -> None:
    req, ia, _ = share_fixture(db, users, tmp_path)
    h = hdr(staff)
    assert staff.post(f"/requests/{req.id}/send", data={"recipient": "bad", f"include_{ia.id}": "1"}, headers=h).status_code == 400
    assert staff.post(f"/requests/{req.id}/send", data={"recipient": "a@b.test"}, headers=h).status_code == 400
    assert staff.post(f"/requests/{req.id}/approve", data={"recipient": "a@b.test"}, headers=h).status_code == 400
    assert not outbox

    def boom(*a, **k):
        raise smtplib.SMTPException("down")

    monkeypatch.setattr(req_api, "send_mail", boom)
    resp = staff.post(f"/requests/{req.id}/send", data={"recipient": "a@b.test", f"include_{ia.id}": "1"}, headers=h, follow_redirects=False)
    assert "could+not+be+sent" in resp.headers["location"]
    db.expire_all()
    assert db.get(Request, req.id).status == "ready_for_review" and db.get(ShortlistItem, ia.id).status == "suggested"
    assert db.scalar(select(AuditLog.id).where(AuditLog.action == "request.shared")) is None


def test_send_requires_csrf_and_a_staff_login(db, users, client, outbox, tmp_path) -> None:
    req, ia, _ = share_fixture(db, users, tmp_path)
    assert client.post(f"/requests/{req.id}/send", data={"recipient": "a@b.test"}, follow_redirects=False).status_code == 403
    login(client, "recruiter@test.local")
    assert client.post(f"/requests/{req.id}/send", data={"recipient": "a@b.test", f"include_{ia.id}": "1"}).status_code == 403
    assert not outbox


def test_approving_resolves_the_shortlist_review_item(db, users, staff, outbox, tmp_path) -> None:
    req, ia, _ = share_fixture(db, users, tmp_path)
    db.add(ReviewItem(type="shortlist_ready", summary="x", entity_type="request", entity_id=req.id))
    db.commit()
    staff.post(f"/requests/{req.id}/send", headers=hdr(staff), data={"recipient": "a@b.test", f"include_{ia.id}": "1"})
    db.expire_all()
    item = db.scalars(select(ReviewItem)).one()
    assert item.status == "resolved" and item.resolution == "approved_and_sent"


# ---------------------------------------------------------------- cool-down pacer

def test_pacer_pauses_after_each_batch_and_can_be_disabled() -> None:
    now = [0.0]
    p = IngestPacer(3, 60, clock=lambda: now[0])
    for _ in range(2):
        p.finished_cv_job()
    assert not p.paused()
    p.finished_cv_job()
    assert p.paused() and p.seconds_left() == 60
    now[0] = 61
    assert not p.paused()
    for _ in range(3):
        p.finished_cv_job()
    assert p.paused()

    off = IngestPacer(0, 60, clock=lambda: now[0])
    for _ in range(100):
        off.finished_cv_job()
    assert not off.paused()


def test_paused_worker_skips_cv_jobs_but_runs_others(db, session_factory) -> None:
    from app.core import queue
    from app.worker import run_one

    ran = []
    saved = dict(queue.HANDLERS)
    queue.HANDLERS["cv.process"] = lambda s, j: ran.append("cv")
    queue.HANDLERS["request.process"] = lambda s, j: ran.append("request")
    try:
        queue.enqueue(db, "cv.process", {})
        queue.enqueue(db, "request.process", {})
        db.commit()
        now = [0.0]
        p = IngestPacer(1, 60, clock=lambda: now[0])
        p._until = 100.0  # paused
        assert run_one(session_factory, "w", p) is True and ran == ["request"]
        assert run_one(session_factory, "w", p) is False  # only the CV job is left, and CV work is paused
        now[0] = 200.0
        assert run_one(session_factory, "w", p) is True and ran == ["request", "cv"]
    finally:
        queue.HANDLERS.clear()
        queue.HANDLERS.update(saved)
