"""Process settings, read from environment variables prefixed ``CLOUDOPTICS_``."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLOUDOPTICS_", extra="ignore")

    environment: Literal["local", "test", "staging", "production"] = "local"
    log_level: str = "INFO"
    service_name: str = "cloudoptics-api"


@lru_cache
def get_settings() -> Settings:
    return Settings()
