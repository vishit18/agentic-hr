"""Test setup. All DB tests run against a throw-away database (hub_test), never the main one.

The database is dropped and recreated at the start of the session and dropped at the end.
Every helper refuses to touch a database whose name does not start with "hub_test".
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.core.auth import hash_password
from app.core.db import get_db
from app.core.models import Base, User
from app.main import app

BACKEND_DIR = Path(__file__).resolve().parents[1]
TEST_DB_NAME = "hub_test"


def _guard(name: str) -> str:
    """Never allow destructive operations on anything but a test database."""
    main_db = make_url(settings.database_url).database
    if not name.startswith("hub_test") or name == main_db:
        raise RuntimeError(f"Refusing to use database '{name}' for tests")
    return name


def url_for(name: str) -> URL:
    return make_url(settings.database_url).set(database=_guard(name))


def create_database(name: str) -> None:
    _guard(name)
    admin = create_engine(make_url(settings.database_url).set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()


def drop_database(name: str) -> None:
    _guard(name)
    admin = create_engine(make_url(settings.database_url).set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def alembic_config(name: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    url = url_for(name).render_as_string(hide_password=False).replace("%", "%%")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(scope="session")
def test_engine() -> Iterator:
    create_database(TEST_DB_NAME)
    command.upgrade(alembic_config(TEST_DB_NAME), "head")
    engine = create_engine(url_for(TEST_DB_NAME))
    yield engine
    engine.dispose()
    drop_database(TEST_DB_NAME)


@pytest.fixture(scope="session")
def session_factory(test_engine) -> "sessionmaker[Session]":
    return sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture()
def db(test_engine, session_factory) -> Iterator[Session]:
    """A session on the test database, with every table emptied first."""
    assert test_engine.url.database == TEST_DB_NAME
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with test_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    with session_factory() as session:
        yield session


@pytest.fixture()
def client(db, session_factory):
    """TestClient wired to the test database, with a clean login rate limiter."""
    from fastapi.testclient import TestClient

    from app.api.auth import limiter

    def override_db() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    app.state.session_factory = session_factory
    limiter.clear()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    from app.core.db import SessionLocal

    app.state.session_factory = SessionLocal
    limiter.clear()


PASSWORD = "correct horse battery"


@pytest.fixture()
def users(db) -> dict[str, User]:
    made: dict[str, User] = {}
    for role in ("recruiter", "hr_admin", "engineer"):
        user = User(
            email=f"{role}@test.local",
            name=f"Test {role}",
            password_hash=hash_password(PASSWORD),
            role=role,
        )
        db.add(user)
        made[role] = user
    db.commit()
    return made


@pytest.fixture(autouse=True)
def ai_is_up(monkeypatch):
    """Pages check whether the AI service is reachable (for the banner). Tests never touch the network."""
    from app.api import activity
    from app.core import web

    monkeypatch.setattr(web, "ai_available", lambda: True)
    monkeypatch.setattr(activity, "ai_available", lambda: True)
