from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings, read from environment variables prefixed with DOTA_INSIGHTS_."""

    model_config = SettingsConfigDict(env_prefix="DOTA_INSIGHTS_", env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8000
    opendota_api_key: str | None = None
    stratz_api_token: str | None = None
    database_url: str | None = None


settings = Settings()
