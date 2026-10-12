from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CAREERFORGE_", extra="ignore")
    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://careerforge:careerforge_dev@127.0.0.1:55432/careerforge_test?connect_timeout=5"
    )
    allowed_origins: list[str] = ["http://127.0.0.1:5173", "http://127.0.0.1:8000"]
    cookie_secure: bool = False
    session_hours: int = Field(default=24, ge=1, le=168)
    max_source_chars: int = Field(default=100000, ge=1)
    auth_mode: str = "session"
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:56379/0")


@lru_cache
def get_settings() -> Settings:
    return Settings()
