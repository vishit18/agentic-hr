import re

import pytest
from sqlalchemy import select

from app.core.auth import hash_password, verify_password
from app.core.models import AuditLog, ReviewItem
from tests.conftest import PASSWORD

TOKEN_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
META_RE = re.compile(r'name="csrf-token" content="([^"]+)"')


def login(client, email: str, password: str = PASSWORD):
    page = client.get("/login")
    token = TOKEN_RE.search(page.text).group(1)
    return client.post(
        "/login", data={"email": email, "password": password, "csrf_token": token}, follow_redirects=False
    )


def page_token(client, path: str = "/needs-you") -> str:
    return META_RE.search(client.get(path).text).group(1)


def test_argon2_round_trip() -> None:
    h = hash_password("s3cret pass")
    assert h.startswith("$argon2")
    assert verify_password(h, "s3cret pass")
    assert not verify_password(h, "wrong")
    assert not verify_password("not-a-hash", "anything")


def test_login_success_redirects_and_sets_httponly_cookie(client, users) -> None:
    resp = login(client, "recruiter@test.local")
    assert resp.status_code == 303 and resp.headers["location"] == "/needs-you"
    assert "httponly" in resp.headers["set-cookie"].lower()
    assert client.get("/needs-you").status_code == 200


def test_wrong_password_and_unknown_email_give_the_same_message(client, users) -> None:
    bad_pw = login(client, "recruiter@test.local", "nope")
    unknown = login(client, "nobody@test.local", "nope")
    assert bad_pw.status_code == unknown.status_code == 401
    assert "Email or password is incorrect." in bad_pw.text
    assert "Email or password is incorrect." in unknown.text


def test_login_is_rate_limited_even_with_the_right_password(client, users) -> None:
    for _ in range(5):
        assert login(client, "recruiter@test.local", "wrong").status_code == 401
    blocked = login(client, "recruiter@test.local", PASSWORD)
    assert blocked.status_code == 429
    assert "Too many failed attempts" in blocked.text


def test_login_events_are_audited_without_raw_email(db, client, users) -> None:
    login(client, "recruiter@test.local", "wrong")
    login(client, "recruiter@test.local")
    db.expire_all()
    actions = [a.action for a in db.scalars(select(AuditLog).order_by(AuditLog.id))]
    assert "login.failed" in actions and "login.ok" in actions
    failed = db.scalars(select(AuditLog).where(AuditLog.action == "login.failed")).one()
    assert "recruiter@test.local" not in str(failed.output) + str(failed.actor_name)


def test_protected_page_redirects_to_login_when_anonymous(client) -> None:
    resp = client.get("/needs-you", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/login"


def test_htmx_request_when_anonymous_gets_401_with_redirect_header(client) -> None:
    resp = client.get("/needs-you", headers={"HX-Request": "true"}, follow_redirects=False)
    assert resp.status_code == 401 and resp.headers["hx-redirect"] == "/login"


def test_post_without_csrf_token_is_rejected(client, users) -> None:
    assert client.post("/login", data={"email": "recruiter@test.local", "password": PASSWORD}).status_code == 403
    login(client, "recruiter@test.local")
    assert client.post("/logout", follow_redirects=False).status_code == 403
    assert client.get("/needs-you").status_code == 200  # still logged in


def test_post_with_wrong_csrf_token_is_rejected(client, users) -> None:
    login(client, "recruiter@test.local")
    resp = client.post("/logout", headers={"X-CSRF-Token": "forged"}, follow_redirects=False)
    assert resp.status_code == 403


def test_logout_with_header_token_works(client, users) -> None:
    login(client, "recruiter@test.local")
    token = page_token(client)
    resp = client.post("/logout", headers={"X-CSRF-Token": token}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/login"
    assert client.get("/needs-you", follow_redirects=False).status_code == 303


def test_forged_session_cookie_is_ignored(client, users) -> None:
    client.cookies.set("cadence_session", "forged.value.here")
    assert client.get("/needs-you", follow_redirects=False).status_code == 303


def test_inactive_user_cannot_log_in(db, client, users) -> None:
    users["recruiter"].is_active = False
    db.commit()
    assert login(client, "recruiter@test.local").status_code == 401


@pytest.mark.parametrize(
    "role,path,expected",
    [
        ("recruiter", "/tickets", 403),
        ("hr_admin", "/tickets", 403),
        ("engineer", "/tickets", 200),
        ("recruiter", "/settings", 403),
        ("engineer", "/settings", 403),
        ("hr_admin", "/settings", 200),
        ("recruiter", "/candidates", 200),
        ("recruiter", "/requests", 200),
        ("recruiter", "/activity", 200),
        ("recruiter", "/reports", 200),
    ],
)
def test_role_access(client, users, role, path, expected) -> None:
    login(client, f"{role}@test.local")
    assert client.get(path).status_code == expected


def test_nav_shows_only_what_the_role_may_open(client, users) -> None:
    login(client, "recruiter@test.local")
    html = client.get("/needs-you").text
    assert 'href="/candidates"' in html
    assert 'href="/tickets"' not in html and 'href="/settings"' not in html

    client.cookies.clear()
    login(client, "engineer@test.local")
    assert 'href="/tickets"' in client.get("/needs-you").text

    client.cookies.clear()
    login(client, "hr_admin@test.local")
    assert 'href="/settings"' in client.get("/needs-you").text


def test_shell_has_required_regions_and_empty_state(client, users) -> None:
    login(client, "recruiter@test.local")
    html = client.get("/needs-you").text
    for needle in ("Needs you", "Flag a problem", "Agents idle", 'class="tabbar"', 'class="nav"', "Nothing needs you right now"):
        assert needle in html
    assert META_RE.search(html)


def test_needs_you_badge_counts_open_review_items(db, client, users) -> None:
    db.add_all([ReviewItem(type="file_unreadable", summary="Scan.pdf could not be read", reason="Unreadable"),
                ReviewItem(type="possible_duplicate", summary="Maybe the same person")])
    db.commit()
    login(client, "recruiter@test.local")
    html = client.get("/needs-you").text
    assert 'class="badge">2<' in html
    assert "Scan.pdf could not be read" in html


def test_html_in_review_summary_is_escaped(db, client, users) -> None:
    db.add(ReviewItem(type="low_confidence", summary="<script>alert(1)</script>"))
    db.commit()
    login(client, "recruiter@test.local")
    html = client.get("/needs-you").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_status_page_and_health_are_public(client) -> None:
    assert client.get("/").status_code == 200
    assert "System status" in client.get("/status").text
    assert client.get("/health").status_code == 200
