"""Phase 3: engineer access, truncation flag, Needs-you inbox, merge / undo / unlink."""
import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.agents.extractor import ExpEntry, Extracted
from app.config import settings
from app.core.audit import audit
from app.core.models import (
    AuditLog,
    Candidate,
    CandidateIdentifier,
    CandidateVersion,
    File,
    Job,
    ReviewItem,
    ShortlistItem,
    Request,
)
from app.workflows import merge
from app.workflows.merge import MergeError
from tests.test_auth_shell import login
from tests.test_candidates_ui import add_candidate
from tests.test_cv_intake import cv, env  # noqa: F401 - env is a fixture


def csrf(client, path="/home") -> str:
    return re.search(r'name="csrf-token" content="([^"]+)"', client.get(path, follow_redirects=True).text).group(1)


# ---------------------------------------------------------------- engineer access

ENGINEER_BLOCKED = ["/needs-you", "/needs-you/1", "/candidates", "/candidates/1", "/candidates/new", "/requests",
                    "/requests/1", "/requests/new", "/activity", "/activity?tab=agents", "/reports", "/files/1/original",
                    "/problems", "/merge?keep=1&drop=2", "/reports/export.xlsx", "/settings", "/exports/all"]


@pytest.mark.parametrize("path", ENGINEER_BLOCKED)
def test_engineer_cannot_open_candidate_request_or_hr_pages(client, users, path) -> None:
    login(client, "engineer@test.local")
    assert client.get(path, follow_redirects=False).status_code == 403


def test_engineer_lands_on_tickets_and_sees_only_tickets(client, users) -> None:
    resp = login(client, "engineer@test.local")
    assert resp.headers["location"] == "/tickets"
    page = client.get("/tickets")
    assert page.status_code == 200
    assert 'href="/tickets"' in page.text
    for hidden in ('href="/candidates"', 'href="/requests"', 'href="/needs-you"', 'href="/activity"',
                   'href="/reports"', 'Search candidates', 'Flag a problem'):
        assert hidden not in page.text
    assert client.get("/home", follow_redirects=False).headers["location"] == "/tickets"


def test_engineer_cannot_post_to_hr_actions(client, users, db) -> None:
    cand = add_candidate(db, "Some One")
    login(client, "engineer@test.local")
    h = {"X-CSRF-Token": csrf(client, "/tickets")}
    assert client.post("/merge", data={"keep": cand.id, "drop": cand.id}, headers=h).status_code == 403
    assert client.post("/problems", data={"message": "x y z"}, headers=h).status_code == 403
    assert client.post("/requests/links", data={"company": "X"}, headers=h).status_code == 403
    assert client.post("/candidates/new", data={"name": "A B"}, headers=h).status_code == 403


def test_other_roles_keep_their_pages_and_lose_tickets(client, users) -> None:
    login(client, "recruiter@test.local")
    assert client.get("/home", follow_redirects=False).headers["location"] == "/needs-you"
    for path in ("/needs-you", "/candidates", "/requests", "/activity", "/reports", "/problems"):
        assert client.get(path).status_code == 200, path
    for path in ("/tickets", "/settings", "/exports/all"):
        assert client.get(path).status_code == 403, path


def test_root_redirects_signed_in_users_and_stays_public_otherwise(client, users) -> None:
    assert client.get("/", follow_redirects=False).status_code == 200  # status page, no login needed
    login(client, "recruiter@test.local")
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/home"
    assert "System status" in client.get("/status").text  # always reachable


# ---------------------------------------------------------------- long CVs are flagged, not silently cut

def test_long_cv_is_marked_possibly_incomplete_and_queued_for_a_person(env, monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_max_cv_chars", 300)
    lines, env.client.extracted = cv()
    lines += [f"Further detail line {i} about responsibilities and achievements in this long document." for i in range(12)]
    env.drop("long.docx", lines)
    env.scan()
    env.drain()
    env.db.expire_all()
    ver = env.db.scalars(select(CandidateVersion)).one()
    assert ver.extraction_status == "incomplete"
    assert ver.data["truncated"]["read_chars"] == 300 and ver.data["truncated"]["total_chars"] > 300
    assert any(i.startswith("Long CV") for i in ver.data["issues"])
    item = env.db.scalars(select(ReviewItem).where(ReviewItem.type == "incomplete_extraction")).one()
    assert item.status == "open" and item.entity_id == ver.candidate_id
    assert item.payload["read_chars"] == 300


def test_short_cv_is_not_flagged(env) -> None:
    lines, env.client.extracted = cv()
    env.drop("short.docx", lines)
    env.scan()
    env.drain()
    env.db.expire_all()
    assert env.db.scalars(select(CandidateVersion)).one().extraction_status == "ok"
    assert env.db.scalar(select(func.count()).select_from(ReviewItem)) == 0


def test_incomplete_badge_shows_on_profile_and_the_item_in_needs_you(db, client, users) -> None:
    cand = add_candidate(db, "Long Cv")
    ver = db.get(CandidateVersion, cand.current_version_id)
    ver.extraction_status = "incomplete"
    db.add(ReviewItem(type="incomplete_extraction", summary="Long Cv: CV is long, details may be missing",
                      reason="Long CV: read 300 of 900", entity_type="candidate", entity_id=cand.id,
                      payload={"read_chars": 300, "total_chars": 900}))
    db.commit()
    login(client, "recruiter@test.local")
    assert "Possibly incomplete" in client.get(f"/candidates/{cand.id}").text
    page = client.get("/needs-you").text
    assert "Long CVs that may be missing details" in page and "Long Cv" in page
    item = db.scalars(select(ReviewItem)).one()
    detail = client.get(f"/needs-you/{item.id}").text
    assert "first 300 of 900 characters" in detail and "I checked it, it is fine" in detail


# ---------------------------------------------------------------- Needs-you inbox

@pytest.fixture()
def staff(client, users):
    login(client, "recruiter@test.local")
    return client


def make_item(db, type_, **kw):
    item = ReviewItem(type=type_, summary=kw.pop("summary", f"{type_} item"), **kw)
    db.add(item)
    db.commit()
    return item


def test_inbox_groups_by_type_with_counts_and_age(db, staff) -> None:
    make_item(db, "file_unreadable", summary="scan.pdf could not be read", reason="No readable text")
    make_item(db, "possible_duplicate", summary="Maybe the same person")
    make_item(db, "job_failed", summary="Task failed")
    html = staff.get("/needs-you").text
    for needle in ("Possible duplicates", "Files that could not be read", "Background tasks that failed",
                   "scan.pdf could not be read", 'class="badge">3<'):
        assert needle in html
    assert html.index("Possible duplicates") < html.index("Files that could not be read") < html.index("Background tasks that failed")


def test_every_item_type_opens_with_its_actions(db, staff) -> None:
    f = File(sha256="c" * 64, original_name="bad.pdf", stored_path="/nowhere", size_bytes=1, origin="cvs_dir")
    db.add(f)
    db.commit()
    expectations = {
        "file_unreadable": (dict(entity_type="file", entity_id=f.id, reason="PDF is corrupt"), ["Mark as handled", "Download the file"]),
        "shortlist_ready": (dict(entity_type="request", entity_id=5), ["Review the shortlist"]),
        "request_needs_info": (dict(entity_type="request", entity_id=5, payload={"draft_question": "Hello Gina"}), ["Hello Gina", "Open the request"]),
        "low_confidence": (dict(entity_type="candidate", entity_id=1), ["I checked it, it is fine"]),
        "job_failed": (dict(entity_type="job", entity_id=1), ["Try again", "Dismiss"]),
    }
    for type_, (kw, needles) in expectations.items():
        item = make_item(db, type_, **kw)
        html = staff.get(f"/needs-you/{item.id}").text
        for n in needles:
            assert n in html, (type_, n)
    assert staff.get("/needs-you/99999").status_code == 404


def test_resolving_records_who_and_what_the_agent_suggested(db, staff, users) -> None:
    item = make_item(db, "low_confidence", payload={"suggestion": {"verdict": "same"}})
    h = {"X-CSRF-Token": csrf(staff)}
    resp = staff.post(f"/needs-you/{item.id}/resolve", data={"resolution": "checked_ok"}, headers=h, follow_redirects=False)
    assert resp.status_code == 303
    db.expire_all()
    item = db.get(ReviewItem, item.id)
    assert item.status == "resolved" and item.resolved_by == users["recruiter"].id and item.resolution == "checked_ok"
    row = db.scalars(select(AuditLog).where(AuditLog.action == "review.resolved")).one()
    assert row.actor_name == "recruiter@test.local" and row.output["agent_suggestion"] == {"verdict": "same"}
    assert "Nothing needs you right now" in staff.get("/needs-you").text
    assert staff.post(f"/needs-you/{item.id}/resolve", data={"resolution": "bogus"}, headers=h).status_code == 400
    assert staff.post(f"/needs-you/{item.id}/resolve", data={"resolution": "handled"}).status_code == 403  # csrf


def test_retry_requeues_a_dead_job_and_resolves_the_item(db, staff) -> None:
    job = Job(type="cv.process", payload={}, status="dead", attempts=3, last_error="boom")
    db.add(job)
    db.commit()
    item = make_item(db, "job_failed", entity_type="job", entity_id=job.id)
    staff.post(f"/needs-you/{item.id}/retry", headers={"X-CSRF-Token": csrf(staff)})
    db.expire_all()
    job = db.get(Job, job.id)
    assert job.status == "queued" and job.attempts == 0 and job.last_error is None
    assert db.get(ReviewItem, item.id).resolution == "retried"


def make_pair(db, same_employer=True):
    old = add_candidate(db, "Sara Ahmed", emails=["sara@old.test"])
    new = add_candidate(db, "Sara Ahmed", emails=["sara@new.test"])
    for c, e in ((old, "Globex"), (new, "Globex" if same_employer else "Initech")):
        v = db.get(CandidateVersion, c.current_version_id)
        v.data = {**v.data, "experience": [{"company": e, "title": "Accountant", "start": "2018-01-01"}],
                  "phones": []}
    item = make_item(db, "possible_duplicate", summary="Sara Ahmed may already be in the database",
                     entity_type="candidate", entity_id=new.id,
                     reason="Name 100% similar", payload={
                         "incoming_candidate_id": new.id, "existing_candidate_ids": [old.id],
                         "evidence": ["Name 100% similar", "Same employer: globex"],
                         "suggestion": {"verdict": "same", "reasons": ["Same employer and name"]}})
    db.commit()
    return old, new, item


def test_duplicate_review_shows_side_by_side_evidence_and_suggestion(db, staff) -> None:
    old, new, item = make_pair(db)
    html = staff.get(f"/needs-you/{item.id}").text
    for needle in ("Already in the database", "New CV", "Same employer: globex", "AI suggestion (only a suggestion)",
                   "Same person", "Same person (merge)", "Different people", "Not sure, leave it", "sara@old.test", "sara@new.test"):
        assert needle in html, needle
    assert "match</span>" in html and "differs</span>" in html  # employers match, emails differ
    assert f"/merge?keep={old.id}&amp;drop={new.id}&amp;item={item.id}" in html or f"/merge?keep={old.id}&drop={new.id}&item={item.id}" in html


def test_different_people_resolves_without_merging(db, staff) -> None:
    old, new, item = make_pair(db)
    staff.post(f"/needs-you/{item.id}/resolve", data={"resolution": "different_people"}, headers={"X-CSRF-Token": csrf(staff)})
    db.expire_all()
    assert db.get(ReviewItem, item.id).resolution == "different_people"
    assert db.get(Candidate, new.id).merged_into_id is None and db.get(Candidate, old.id).merged_into_id is None


# ---------------------------------------------------------------- merge and undo

def state_of(db, *cand_ids):
    db.expire_all()
    out = {}
    for cid in cand_ids:
        c = db.get(Candidate, cid)
        out[cid] = {
            "merged": c.merged_into_id, "current": c.current_version_id,
            "versions": sorted((v.id, v.version_no) for v in db.scalars(select(CandidateVersion).where(CandidateVersion.candidate_id == cid))),
            "idents": sorted((i.id, i.value) for i in db.scalars(select(CandidateIdentifier).where(CandidateIdentifier.candidate_id == cid))),
        }
    return out


def test_merge_moves_everything_keeps_history_and_resolves_the_review_item(db, users) -> None:
    old, new, item = make_pair(db)
    db.get(CandidateVersion, db.get(Candidate, new.id).current_version_id).applied_at = datetime.now(timezone.utc) + timedelta(days=1)
    db.commit()
    new_ver = new.current_version_id
    row = merge.merge_candidates(db, old.id, new.id, users["recruiter"])
    db.commit()
    db.expire_all()
    assert db.get(Candidate, new.id).merged_into_id == old.id
    assert sorted(v.version_no for v in db.scalars(select(CandidateVersion).where(CandidateVersion.candidate_id == old.id))) == [1, 2]
    assert db.get(Candidate, old.id).current_version_id == new_ver  # the newest CV is current
    assert {i.value for i in db.scalars(select(CandidateIdentifier).where(CandidateIdentifier.candidate_id == old.id))} == {"sara@old.test", "sara@new.test"}
    assert db.get(ReviewItem, item.id).resolution == "merged"
    assert row.action == "candidate.merged" and row.actor_name == "recruiter@test.local" and row.output["snapshot"]["moved_versions"]


def test_undo_merge_restores_the_exact_previous_state(db, users) -> None:
    old, new, item = make_pair(db)
    before = state_of(db, old.id, new.id)
    row = merge.merge_candidates(db, old.id, new.id, users["recruiter"])
    db.commit()
    assert state_of(db, old.id, new.id) != before
    merge.undo_merge(db, row.id, users["recruiter"])
    db.commit()
    assert state_of(db, old.id, new.id) == before
    assert db.get(ReviewItem, item.id).status == "open"
    with pytest.raises(MergeError, match="already been undone"):
        merge.undo_merge(db, row.id, users["recruiter"])


def test_merge_refuses_bad_requests(db, users) -> None:
    a, b = add_candidate(db, "A One"), add_candidate(db, "B Two")
    with pytest.raises(MergeError):
        merge.merge_candidates(db, a.id, a.id, users["recruiter"])
    with pytest.raises(MergeError):
        merge.merge_candidates(db, a.id, 99999, users["recruiter"])
    merge.merge_candidates(db, a.id, b.id, users["recruiter"])
    db.commit()
    with pytest.raises(MergeError, match="already been merged"):
        merge.merge_candidates(db, a.id, b.id, users["recruiter"])


def test_merge_through_the_screens_and_undo_from_activity(db, staff) -> None:
    old, new, item = make_pair(db)
    conf = staff.get(f"/merge?keep={old.id}&drop={new.id}&item={item.id}")
    assert conf.status_code == 200 and "Merge these two profiles?" in conf.text and "Yes, merge them" in conf.text
    token = csrf(staff)
    assert staff.post("/merge", data={"keep": old.id, "drop": new.id}).status_code == 403
    resp = staff.post("/merge", data={"keep": old.id, "drop": new.id}, headers={"X-CSRF-Token": token}, follow_redirects=False)
    assert resp.headers["location"] == f"/candidates/{old.id}?tab=activity&merged=1"
    page = staff.get(resp.headers["location"]).text
    assert "Merged." in page and "Profiles merged" in page and "Undo" in page
    assert staff.get(f"/candidates/{new.id}", follow_redirects=False).headers["location"] == f"/candidates/{old.id}"
    audit_id = db.scalars(select(AuditLog).where(AuditLog.action == "candidate.merged")).one().id
    undo = staff.post(f"/candidates/{old.id}/undo/{audit_id}", headers={"X-CSRF-Token": token}, follow_redirects=False)
    assert "undone=1" in undo.headers["location"]
    db.expire_all()
    assert db.get(Candidate, new.id).merged_into_id is None
    again = staff.post(f"/candidates/{old.id}/undo/{audit_id}", headers={"X-CSRF-Token": token}, follow_redirects=False)
    assert "error=" in again.headers["location"]


def two_version_candidate(db):
    cand = add_candidate(db, "Multi Version", emails=["mv@x.test"])
    v1 = db.get(CandidateVersion, cand.current_version_id)
    v2 = CandidateVersion(candidate_id=cand.id, version_no=2, full_name="Multi Version", search_text="x", auto_linked=True,
                          applied_at=datetime.now(timezone.utc), data={"name": "Multi Version", "emails": ["mv@x.test", "other@x.test"], "phones": []})
    db.add(v2)
    db.flush()
    cand.current_version_id = v2.id
    db.commit()
    return cand, v1, v2


def test_unlink_an_auto_linked_cv_and_undo(db, users) -> None:
    cand, v1, v2 = two_version_candidate(db)
    before = state_of(db, cand.id)
    row = merge.unlink_version(db, v2.id, users["recruiter"])
    db.commit()
    new_id = row.output["snapshot"]["new_candidate_id"]
    db.expire_all()
    assert db.get(CandidateVersion, v2.id).candidate_id == new_id and db.get(CandidateVersion, v2.id).auto_linked is False
    assert db.get(Candidate, cand.id).current_version_id == v1.id
    assert db.get(Candidate, new_id).current_version_id == v2.id
    merge.undo_unlink(db, row.id, users["recruiter"])
    db.commit()
    db.expire_all()
    assert state_of(db, cand.id) == before
    assert db.get(CandidateVersion, v2.id).auto_linked is True and db.get(Candidate, new_id) is None


def test_unlink_needs_two_cvs_and_undo_is_refused_once_the_new_profile_is_used(db, users) -> None:
    solo = add_candidate(db, "Only One")
    with pytest.raises(MergeError, match="only CV"):
        merge.unlink_version(db, solo.current_version_id, users["recruiter"])
    cand, v1, v2 = two_version_candidate(db)
    row = merge.unlink_version(db, v2.id, users["recruiter"])
    db.commit()
    req = Request(reference="R-1", company="X", data={}, status="new")
    db.add(req)
    db.flush()
    db.add(ShortlistItem(request_id=req.id, candidate_id=row.output["snapshot"]["new_candidate_id"], rank=1, status="suggested"))
    db.commit()
    with pytest.raises(MergeError, match="used since"):
        merge.undo_unlink(db, row.id, users["recruiter"])


def test_unlink_button_only_on_auto_linked_versions_and_works_from_the_ui(db, staff) -> None:
    cand, v1, v2 = two_version_candidate(db)
    html = staff.get(f"/candidates/{cand.id}?tab=versions").text
    assert html.count("Not the same person: unlink") == 1
    resp = staff.post(f"/candidates/{cand.id}/versions/{v2.id}/unlink", headers={"X-CSRF-Token": csrf(staff)}, follow_redirects=False)
    assert resp.status_code == 303 and "unlinked=1" in resp.headers["location"]
    assert "its own profile" in staff.get(resp.headers["location"]).text
    assert staff.post(f"/candidates/{cand.id}/versions/99999/unlink", headers={"X-CSRF-Token": csrf(staff)}).status_code == 404
