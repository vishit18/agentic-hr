"""The test helpers must never be able to point at the main database."""
import pytest

from app.config import settings
from tests.conftest import _guard, create_database, url_for
from sqlalchemy.engine import make_url


def test_guard_refuses_main_database() -> None:
    main = make_url(settings.database_url).database
    with pytest.raises(RuntimeError):
        _guard(main)
    with pytest.raises(RuntimeError):
        create_database(main)
    with pytest.raises(RuntimeError):
        url_for("postgres")


def test_guard_accepts_test_names() -> None:
    assert _guard("hub_test") == "hub_test"
    assert _guard("hub_test_migrations") == "hub_test_migrations"
