from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CAREERFORGE_",
        extra="ignore",
        env_file=Path(__file__).resolve().parents[4] / ".env",
        env_file_encoding="utf-8",
    )
    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://careerforge:careerforge_dev@127.0.0.1:55432/careerforge_test?connect_timeout=5"
    )
    allowed_origins: list[str] = ["http://127.0.0.1:5173", "http://127.0.0.1:8000"]
    cookie_secure: bool = False
    session_hours: int = Field(default=24, ge=1, le=168)
    max_source_chars: int = Field(default=100000, ge=1)
    auth_mode: str = "session"
    job_contract_write: bool = True
    job_contract_read: Literal["off", "shadow", "on"] = "on"
    real_agent_enabled: bool = False
    model_name: str = Field(
        default="gpt-6.1-sol", validation_alias=AliasChoices("CAREERFORGE_MODEL_NAME", "MODEL_NAME")
    )
    openai_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("CAREERFORGE_OPENAI_API_KEY", "OPENAI_API_KEY"),
    )
    openai_base_url: str = Field(
        default="https://modelflare.dev/v1",
        validation_alias=AliasChoices("CAREERFORGE_OPENAI_BASE_URL", "OPENAI_BASE_URL"),
    )
    model_reasoning_effort: Literal["low"] = "low"
    model_max_output_tokens: int = Field(default=2048, ge=128, le=2048)
    real_agent_timeout_seconds: int = Field(default=120, ge=10, le=180)
    tavily_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("CAREERFORGE_TAVILY_API_KEY", "TAVILY_API_KEY"),
    )
    maintenance_enabled: bool = False
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:56379/0")


@lru_cache
def get_settings() -> Settings:
    return Settings()
