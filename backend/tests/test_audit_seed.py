from sqlalchemy import func, select

from app.config import settings
from app.core.audit import audit, hash_input
from app.core.auth import verify_password
from app.core.models import AuditLog, User
from app.core.seed import seed_users


def test_audit_stores_hash_not_raw_input(db) -> None:
    secret_cv_text = "Jane Doe, jane@example.com, CFA, 8 years at Bank X"
    audit(db, actor_type="agent", actor_name="extractor", action="extract", input=secret_cv_text,
          output={"fields": 3}, model="qwen2.5:7b", prompt_version="extract-v1", latency_ms=42)
    db.commit()

    row = db.scalars(select(AuditLog)).one()
    assert row.input_hash == hash_input(secret_cv_text)
    assert len(row.input_hash) == 64
    assert row.model == "qwen2.5:7b" and row.prompt_version == "extract-v1" and row.latency_ms == 42
    dumped = " ".join(str(v) for v in (row.output, row.actor_name, row.action, row.input_hash))
    assert "jane@example.com" not in dumped


def test_audit_rolls_back_with_the_step(db) -> None:
    audit(db, actor_type="system", action="half-done")
    db.rollback()
    assert db.scalar(select(func.count()).select_from(AuditLog)) == 0


def test_hash_is_stable_for_json_values() -> None:
    assert hash_input({"a": 1, "b": 2}) == hash_input({"b": 2, "a": 1})


def test_seed_is_idempotent_and_never_overwrites(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "seed_recruiter_password", "first-pass")
    monkeypatch.setattr(settings, "seed_admin_password", "first-pass")
    monkeypatch.setattr(settings, "seed_engineer_password", "first-pass")

    assert seed_users(db) == 3
    assert seed_users(db) == 0
    assert db.scalar(select(func.count()).select_from(User)) == 3

    monkeypatch.setattr(settings, "seed_recruiter_password", "different-pass")
    assert seed_users(db) == 0
    recruiter = db.scalars(select(User).where(User.role == "recruiter")).one()
    assert verify_password(recruiter.password_hash, "first-pass")
    assert recruiter.password_hash.startswith("$argon2")


def test_seed_skips_accounts_without_a_password(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "seed_recruiter_password", "")
    monkeypatch.setattr(settings, "seed_admin_password", "x-pass")
    monkeypatch.setattr(settings, "seed_engineer_password", "")
    assert seed_users(db) == 1
