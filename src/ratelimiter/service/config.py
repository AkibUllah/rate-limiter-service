from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "rate-limiter-service"
    environment: str = "development"
    log_level: str = "INFO"

    redis_url: str | None = None
    key_prefix: str = "rl:"
    api_key: str | None = None

    default_policy: str = "60/minute;algorithm=sliding_counter"
    service_policy: str | None = "1000/minute;algorithm=token_bucket"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
