"""Test-wide settings: no shared Redis, no Telegram alerts, no per-client rate limits, no database.

Set before ``dota_insights.config`` is imported, so a developer's ``.env`` (environment variables
win over it) can never point tests at live services. Tests that need these features install
fakes (``tests/fakes.py``), override ``settings`` with ``monkeypatch`` or opt into a database.
"""

import os

for name in (
    "UPSTASH_REDIS_REST_URL",
    "UPSTASH_REDIS_REST_TOKEN",
    "KV_REST_API_URL",
    "KV_REST_API_TOKEN",
    "DOTA_INSIGHTS_REDIS_REST_URL",
    "DOTA_INSIGHTS_REDIS_REST_TOKEN",
    "DOTA_INSIGHTS_TELEGRAM_BOT_TOKEN",
    "DOTA_INSIGHTS_TELEGRAM_CHAT_ID",
):
    os.environ[name] = ""
os.environ["DOTA_INSIGHTS_RATE_LIMIT_ENABLED"] = "false"

import pytest  # noqa: E402

from dota_insights.db.access import use_database  # noqa: E402


@pytest.fixture(autouse=True)
def no_database():
    """Tools read no database unless a test opts in (the developer's .env may name one)."""
    with use_database(None):
        yield
