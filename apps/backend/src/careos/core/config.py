"""Application configuration.

All configuration comes from environment variables prefixed with ``CAREOS_``.
Secrets are never hard-coded; development defaults are rejected in production.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


_INSECURE_MARKERS = ("insecure", "change-me", "replace-with", "dev-only")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CAREOS_",
        env_file=None,
        extra="ignore",
        case_sensitive=False,
    )

    environment: Environment = Environment.DEVELOPMENT
    log_level: str = "INFO"
    log_json: bool = True

    # --- persistence -----------------------------------------------------------
    database_url: SecretStr = SecretStr("postgresql+asyncpg://careos:careos@localhost:5432/careos")
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_echo: bool = False
    redis_url: str | None = "redis://localhost:6379/0"

    # --- security ----------------------------------------------------------------
    secret_key: SecretStr = SecretStr("dev-insecure-secret-key-do-not-use-in-production")
    session_ttl_minutes: int = Field(default=720, ge=5, le=24 * 60)
    session_idle_timeout_minutes: int = Field(default=120, ge=5, le=24 * 60)
    cookie_secure: bool = False
    cookie_domain: str | None = None
    cors_allowed_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]
    login_max_attempts: int = Field(default=10, ge=1)
    login_window_seconds: int = Field(default=300, ge=10)
    gateway_max_requests_per_minute: int = Field(default=600, ge=1)

    # --- device gateway / simulator ------------------------------------------------
    simulator_enabled: bool = True
    simulator_gateway_key: SecretStr | None = None
    gateway_internal_url: str = "http://localhost:8000"

    # --- providers -----------------------------------------------------------------
    voice_provider: Literal["mock", "disabled"] = "mock"
    notification_provider: Literal["mock", "disabled"] = "mock"
    ai_provider: Literal["mock", "mock_unavailable", "disabled"] = "mock"
    mock_voice_outcome: Literal["no_answer", "answered_acknowledged", "failure"] = "no_answer"
    provider_timeout_seconds: float = Field(default=10.0, gt=0, le=120)

    # --- worker / escalation ---------------------------------------------------------
    worker_poll_interval_seconds: float = Field(default=1.0, gt=0, le=60)
    worker_batch_size: int = Field(default=20, ge=1, le=500)
    worker_lease_seconds: int = Field(default=60, ge=5)
    escalation_max_attempts: int = Field(default=3, ge=1, le=10)
    escalation_retry_base_seconds: float = Field(default=5.0, ge=0)
    device_offline_after_seconds: int = Field(default=86_400, ge=60)
    device_low_battery_threshold: int = Field(default=20, ge=1, le=100)
    device_sweep_interval_seconds: float = Field(default=30.0, gt=0)
    #: Development convenience: run the worker loops inside the API process (single process,
    #: no Redis needed for live console updates). Never used in staging/production.
    embedded_worker: bool = False

    # --- demo seed -------------------------------------------------------------------
    seed_demo_password: SecretStr | None = None

    metrics_enabled: bool = True

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("cookie_domain", "redis_url", mode="before")
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        return value or None

    @model_validator(mode="after")
    def _enforce_production_safety(self) -> Settings:
        secret = self.secret_key.get_secret_value()
        if len(secret) < 32:
            raise ValueError("CAREOS_SECRET_KEY must be at least 32 characters")
        if self.environment in (Environment.STAGING, Environment.PRODUCTION):
            problems: list[str] = []
            if any(marker in secret.lower() for marker in _INSECURE_MARKERS):
                problems.append("CAREOS_SECRET_KEY uses a development placeholder")
            if not self.cookie_secure:
                problems.append("CAREOS_COOKIE_SECURE must be true")
            if self.simulator_enabled:
                problems.append("CAREOS_SIMULATOR_ENABLED must be false")
            if self.embedded_worker:
                problems.append("CAREOS_EMBEDDED_WORKER must be false (run a separate worker)")
            if "*" in self.cors_allowed_origins:
                problems.append("CAREOS_CORS_ALLOWED_ORIGINS must not contain '*'")
            if problems:
                raise ValueError("Unsafe production configuration: " + "; ".join(problems))
        return self

    @property
    def is_production_like(self) -> bool:
        return self.environment in (Environment.STAGING, Environment.PRODUCTION)


@lru_cache
def get_settings() -> Settings:
    return Settings()
