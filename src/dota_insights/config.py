from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings, read from environment variables prefixed with DOTA_INSIGHTS_."""

    model_config = SettingsConfigDict(env_prefix="DOTA_INSIGHTS_", env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8000
    opendota_api_key: str | None = None
    stratz_api_token: str | None = None
    # The Neon integration on Vercel injects a plain DATABASE_URL.
    database_url: str | None = Field(
        default=None, validation_alias=AliasChoices("DOTA_INSIGHTS_DATABASE_URL", "DATABASE_URL")
    )
    log_level: str = "INFO"

    # Shared state across serverless instances: Upstash Redis over REST. The Vercel Upstash
    # integration injects UPSTASH_REDIS_REST_* (older KV stores: KV_REST_API_*). Without them,
    # caches, quotas and rate limits stay per instance.
    redis_rest_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "DOTA_INSIGHTS_REDIS_REST_URL", "UPSTASH_REDIS_REST_URL", "KV_REST_API_URL"
        ),
    )
    redis_rest_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "DOTA_INSIGHTS_REDIS_REST_TOKEN", "UPSTASH_REDIS_REST_TOKEN", "KV_REST_API_TOKEN"
        ),
    )
    redis_timeout: float = 1.5
    # After a Redis failure, skip it for this many seconds and use the in-memory fallbacks.
    redis_cooldown: float = 30.0
    # Larger values stay in the instance's own cache only.
    cache_shared_max_bytes: int = 512 * 1024

    # Server-wide caps on source calls, shared by all instances through Redis. They leave headroom
    # under the providers' limits (OpenDota free: 60/min, 3,000/day per IP; STRATZ token: 250/min,
    # 10,000/day, also spent by the collector).
    opendota_server_per_minute: int = 50
    opendota_server_daily: int = 2500
    stratz_server_per_minute: int = 120
    stratz_server_daily: int = 4000

    # Per-client limits on tool calls (client IP from x-real-ip / x-forwarded-for on Vercel).
    # Heavy tools (explorer queries, multi-hero aggregates) also count against the general limits.
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = 30
    rate_limit_per_day: int = 1000
    rate_limit_heavy_per_minute: int = 5
    rate_limit_heavy_per_day: int = 150

    # Reports from clients (report_wrong_answer, suggest_feature), per client IP.
    rate_limit_feedback_per_minute: int = 3
    rate_limit_feedback_per_day: int = 30

    # Usage analytics (dota_insights.usage): one row per tool call, queued in Redis and moved to
    # Postgres by the collector's ``usage`` job, which also deletes rows past the retention.
    usage_enabled: bool = True
    # Bounds the time recording may add to a tool call; a slower write is dropped.
    usage_write_timeout: float = 0.5
    # Redis queue cap: when the collector stops flushing, the oldest events are dropped first.
    usage_queue_max: int = 50_000
    usage_retention_days: int = 90
    feedback_retention_days: int = 365
    # Optional secret mixed into client hashes, next to the weekly salt kept in Redis.
    usage_hash_secret: str | None = None
    # Bearer token for GET /admin/usage; unset, the route answers 404.
    admin_token: str | None = None

    # Optional Telegram alerts on quota exhaustion and error-rate spikes.
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    alert_throttle_seconds: int = 1800
    alert_quota_ratio: float = 0.8
    alert_error_rate: float = 0.5
    alert_error_min_calls: int = 10

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
