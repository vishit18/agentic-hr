"""Idempotent seed: creates the demo accounts from SEED_* env vars if they do not exist.

Never overwrites an existing user's password. Skips any account whose password is not set.
Run with: python -m app.core.seed
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.audit import audit
from app.core.auth import hash_password
from app.core.db import SessionLocal
from app.core.models import User

log = logging.getLogger("seed")


def demo_accounts() -> list[tuple[str, str, str, str]]:
    """(email, password, name, role)"""
    return [
        (settings.seed_recruiter_email, settings.seed_recruiter_password, "Demo Recruiter", "recruiter"),
        (settings.seed_admin_email, settings.seed_admin_password, "Demo HR Admin", "hr_admin"),
        (settings.seed_engineer_email, settings.seed_engineer_password, "Demo Engineer", "engineer"),
    ]


def seed_users(session: Session) -> int:
    created = 0
    for email, password, name, role in demo_accounts():
        email = email.strip().lower()
        if not email or not password:
            log.warning("Seed: skipping %s account (email or password not set)", role)
            continue
        if session.scalar(select(User).where(User.email == email)):
            continue
        user = User(email=email, name=name, password_hash=hash_password(password), role=role)
        session.add(user)
        session.flush()
        audit(
            session,
            actor_type="system",
            actor_name="seed",
            action="user.seeded",
            entity_type="user",
            entity_id=user.id,
            output={"role": role},
        )
        created += 1
    session.commit()
    return created


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    with SessionLocal() as session:
        created = seed_users(session)
    log.info("Seed finished: %s new user(s).", created)


if __name__ == "__main__":
    main()
