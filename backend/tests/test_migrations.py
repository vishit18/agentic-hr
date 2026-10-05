"""Migrations run against their own throw-away database, never the main one."""
from alembic import command
from sqlalchemy import create_engine, inspect, text

from app.core.models import Base
from tests.conftest import alembic_config, create_database, drop_database, url_for

DB = "hub_test_migrations"
EXPECTED = {t.name for t in Base.metadata.sorted_tables}
REQUIRED = {
    "users", "files", "candidates", "candidate_versions", "candidate_embeddings", "requests",
    "request_requirements", "shortlist_items", "review_items", "audit_log", "jobs", "tickets",
    "notes", "attachments",
}


def _tables(engine) -> set[str]:
    return set(inspect(engine).get_table_names()) - {"alembic_version"}


def test_upgrade_downgrade_upgrade() -> None:
    create_database(DB)
    engine = create_engine(url_for(DB))
    try:
        cfg = alembic_config(DB)
        command.upgrade(cfg, "head")
        assert REQUIRED <= _tables(engine)
        assert _tables(engine) == EXPECTED

        with engine.connect() as conn:
            assert conn.scalar(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")) == 1

        command.downgrade(cfg, "base")
        assert _tables(engine) == set()

        command.upgrade(cfg, "head")
        assert _tables(engine) == EXPECTED
    finally:
        engine.dispose()
        drop_database(DB)


def test_models_match_migration() -> None:
    """Autogenerate would find no differences between the models and the migration."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    create_database(DB)
    engine = create_engine(url_for(DB))
    try:
        command.upgrade(alembic_config(DB), "head")
        with engine.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
        assert diff == []
    finally:
        engine.dispose()
        drop_database(DB)
