"""Application settings, loaded from environment variables and the repo-root .env file."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    openai_api_key: SecretStr | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4.1", alias="OPENAI_MODEL")
    # Sent only to reasoning models (gpt-5*, o*); "" disables.
    openai_reasoning_effort: str = Field(default="low", alias="OPENAI_REASONING_EFFORT")
    openai_timeout_s: float = Field(default=60, alias="OPENAI_TIMEOUT_S")

    data_dir: Path = Field(default=REPO_ROOT / "data", alias="DATA_DIR")
    invoices_file: str = "invoices_2025_clean.csv"
    vendors_file: str = "vendor_master.csv"
    policy_file: str = "procurement_policy.md"

    var_dir: Path = Field(default=REPO_ROOT / "var", alias="VAR_DIR")

    fx_refresh_on_startup: bool = Field(default=True, alias="FX_REFRESH_ON_STARTUP")
    # Startup refresh only runs when a cached snapshot is missing or older than this.
    fx_max_age_hours: float = Field(default=24, alias="FX_MAX_AGE_HOURS")

    dev_mode: bool = Field(default=True, alias="DEV_MODE")
    # Each /ask can make up to ~6 model calls; extra concurrent questions get HTTP 503 + Retry-After.
    max_concurrent_asks: int = Field(default=4, alias="MAX_CONCURRENT_ASKS", ge=1)
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @property
    def invoices_path(self) -> Path:
        return self.data_dir / self.invoices_file

    @property
    def vendors_path(self) -> Path:
        return self.data_dir / self.vendors_file

    @property
    def policy_path(self) -> Path:
        return self.data_dir / self.policy_file

    @property
    def fx_cache_dir(self) -> Path:
        return self.var_dir / "fx_cache" / "ecb"

    @property
    def traces_dir(self) -> Path:
        return self.var_dir / "traces"


@lru_cache
def get_settings() -> Settings:
    return Settings()
