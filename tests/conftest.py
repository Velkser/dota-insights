import pytest

from dota_insights.db.access import use_database


@pytest.fixture(autouse=True)
def no_database():
    """Tools read no database unless a test opts in (the developer's .env may name one)."""
    with use_database(None):
        yield
