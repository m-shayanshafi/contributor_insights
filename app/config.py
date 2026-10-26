"""Application settings, loaded from environment variables and `.env`."""

import re
from enum import StrEnum
from pathlib import Path

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.analytics.periods import REQUIRED_HISTORY_DAYS

REPO_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")


class DataSource(StrEnum):
    GITHUB = "github"
    FIXTURES = "fixtures"


class LLMMode(StrEnum):
    LIVE = "live"
    RECORDED = "recorded"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    target_repo: str = "home-assistant/core"
    data_source: DataSource = DataSource.GITHUB
    llm_mode: LLMMode = LLMMode.LIVE

    # SecretStr keeps values out of repr(), str() and serialized output.
    github_token: SecretStr | None = None
    gemini_api_key: SecretStr | None = None

    gemini_model: str = "gemini-3.5-flash-lite"
    db_path: Path = Path("data/insights.db")
    fixtures_dir: Path = Path("fixtures")
    sync_ttl_seconds: int = 3600
    history_days: int = 180
    max_concurrent_backfills: int = 2
    max_new_contributors_per_hour: int = 20
    rate_limit: str = "30/minute"

    @field_validator("github_token", "gemini_api_key", mode="before")
    @classmethod
    def _blank_is_missing(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("target_repo")
    @classmethod
    def _valid_repo(cls, value: str) -> str:
        if not REPO_PATTERN.fullmatch(value):
            raise ValueError("TARGET_REPO must look like 'owner/name'")
        return value

    @field_validator("history_days")
    @classmethod
    def _enough_history(cls, value: int) -> int:
        if value < REQUIRED_HISTORY_DAYS:
            raise ValueError(f"HISTORY_DAYS must be at least {REQUIRED_HISTORY_DAYS}")
        return value

    @model_validator(mode="after")
    def _required_secrets(self) -> "Settings":
        # Fail fast at startup instead of on the first request.
        if self.data_source is DataSource.GITHUB and self.github_token is None:
            raise ValueError("GITHUB_TOKEN is required when DATA_SOURCE=github")
        if self.llm_mode is LLMMode.LIVE and self.gemini_api_key is None:
            raise ValueError("GEMINI_API_KEY is required when LLM_MODE=live")
        return self

    def secret_values(self) -> list[str]:
        """Raw secret values, used only by the log redaction filter."""
        secrets = (self.github_token, self.gemini_api_key)
        return [s.get_secret_value() for s in secrets if s is not None]
