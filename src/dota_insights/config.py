from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings, read from environment variables prefixed with DOTA_INSIGHTS_."""

    model_config = SettingsConfigDict(env_prefix="DOTA_INSIGHTS_", env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8000
    opendota_api_key: str | None = None
    stratz_api_token: str | None = None
    database_url: str | None = None
    log_level: str = "INFO"

    # Collector (separate process). The OpenDota free tier is 60/min and 3,000/day per IP, shared
    # with the MCP server's live calls, so the collector takes only part of it.
    collector_opendota_daily_budget: int = 1800
    collector_opendota_per_minute: float = 20.0
    collector_stratz_daily_budget: int = 5000
    collector_stratz_hourly_budget: int = 1000
    collector_stratz_per_minute: float = 100.0
    collector_rank_brackets: list[int] = [1, 2, 3, 4, 5, 6, 7]
    collector_max_pages_per_run: int = 10
    collector_explorer_rows_per_call: int = 2000
    collector_enrich_matches_per_call: int = 10
    collector_aggregate_batch_size: int = 2000


settings = Settings()
