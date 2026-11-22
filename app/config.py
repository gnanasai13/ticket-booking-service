from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://tickets:tickets@localhost:5432/tickets"
    jwt_secret: str = "dev-only-change-me"
    access_token_minutes: int = 60
    hold_ttl_seconds: int = 600
    auto_create_schema: bool = False
    redis_url: str | None = None
    rate_limit_requests_per_minute: int = 120
    event_cache_ttl_seconds: int = 30
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
